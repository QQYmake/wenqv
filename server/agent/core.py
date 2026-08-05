"""Cancellable, HTTP-independent streaming ReAct agent loop."""

from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .context import ContextConfig, ContextManager, ContextPreparation
from .models import AgentEvent, ChatMessage, LLMStreamChunk, ToolCall
from .ports import ConversationStore, LLMClient, LLMClientProvider, WorkspaceResolver
from .registry import ToolExecutionContext, ToolExecutionResult, ToolRegistry
from .skills import SkillManager, SkillNotFoundError
from .tools import calculator_tool, load_skill_tool, read_file_tool, remove_skill_tool


class AgentRunCancelled(Exception):
    """Internal control flow raised when an active request is aborted."""


@dataclass(frozen=True, slots=True)
class AgentConfig:
    max_turns: int = 20
    max_tool_retries: int = 2
    tool_timeout_s: float = 60.0
    tool_result_max_chars: int = 16_000

    def __post_init__(self) -> None:
        if self.max_turns < 1:
            raise ValueError("max_turns must be positive")
        if self.max_tool_retries < 0:
            raise ValueError("max_tool_retries cannot be negative")
        if self.tool_timeout_s <= 0:
            raise ValueError("tool_timeout_s must be positive")
        if self.tool_result_max_chars < 64:
            raise ValueError("tool_result_max_chars must be at least 64")


@dataclass(slots=True)
class _ActiveRun:
    request_id: str
    cancel: asyncio.Event


@dataclass(slots=True)
class _ToolCallBuffer:
    id: str = ""
    name: str = ""
    arguments: str = ""


@dataclass(frozen=True, slots=True)
class _CompletedModelTurn:
    content: str
    calls: tuple[ToolCall, ...]


def build_default_registry(skills: SkillManager) -> ToolRegistry:
    """Create the built-ins used to exercise the complete agent loop."""

    return ToolRegistry(
        [
            calculator_tool(),
            read_file_tool(),
            load_skill_tool(skills),
            remove_skill_tool(skills),
        ]
    )


class AgentCore:
    """Application service that owns one complete user-to-assistant turn.

    The class deliberately has no FastAPI, SSE, SQLite, or SDK imports. A web
    adapter consumes :meth:`stream`, serializes :class:`AgentEvent`, and calls
    :meth:`abort` from its cancellation endpoint.
    """

    persists_messages = True

    def __init__(
        self,
        *,
        store: ConversationStore,
        clients: LLMClientProvider,
        skills: SkillManager,
        tools: ToolRegistry | None = None,
        config: AgentConfig = AgentConfig(),
        context_manager: ContextManager | None = None,
        context_config: ContextConfig = ContextConfig(),
        workspace_root: str | Path = ".",
        workspace_resolver: WorkspaceResolver | None = None,
    ) -> None:
        self.store = store
        self.clients = clients
        self.skills = skills
        self.tools = tools if tools is not None else build_default_registry(skills)
        self.config = config
        self.context = context_manager or ContextManager(clients, context_config)
        self.workspace_root = Path(workspace_root).resolve()
        self.workspace_resolver = workspace_resolver
        self._active: dict[str, _ActiveRun] = {}

    def stream(
        self,
        session_id: str,
        message: str,
        selected_skills: Sequence[str] = (),
        *,
        workspace_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        return self._stream(
            session_id,
            message,
            selected_skills,
            workspace_id=workspace_id,
            request_id=request_id,
        )

    def run_stream(
        self,
        session_id: str,
        message: str,
        selected_skills: Sequence[str] = (),
        *,
        workspace_id: str | None = None,
        request_id: str | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Compatibility alias for delivery adapters."""

        return self.stream(
            session_id,
            message,
            selected_skills,
            workspace_id=workspace_id,
            request_id=request_id,
        )

    async def abort(self, session_id: str, request_id: str | None = None) -> bool:
        active = self._active.get(session_id)
        if active is None or (request_id is not None and active.request_id != request_id):
            return False
        active.cancel.set()
        return True

    async def generate_title(self, session_id: str, *, max_chars: int = 48) -> str:
        messages = await self.store.list_messages(session_id)
        return await self.context.generate_title(messages, max_chars=max_chars)

    async def prepare_context(
        self, session_id: str, *, workspace_id: str | None = None
    ) -> ContextPreparation:
        """Prepare and persist the active context for diagnostics/background work."""

        messages = await self.store.list_messages(session_id)
        prepared = await self.context.prepare(messages, workspace_id=workspace_id)
        if prepared.changed:
            await self.store.replace_messages(session_id, prepared.messages)
        return prepared

    async def _stream(
        self,
        session_id: str,
        message: str,
        selected_skills: Sequence[str],
        *,
        workspace_id: str | None,
        request_id: str | None,
    ) -> AsyncIterator[AgentEvent]:
        run_id = request_id or uuid.uuid4().hex
        if not session_id.strip():
            yield _error("invalid_session", "session_id cannot be empty", run_id)
            yield _done("error", run_id, turns=0)
            return
        if not message.strip():
            yield _error("empty_message", "message cannot be empty", run_id)
            yield _done("error", run_id, turns=0)
            return
        if session_id in self._active:
            yield _error(
                "session_busy",
                "This session already has an active run.",
                run_id,
                recoverable=True,
            )
            yield _done("busy", run_id, turns=0)
            return

        active = _ActiveRun(request_id=run_id, cancel=asyncio.Event())
        self._active[session_id] = active
        turns = 0
        try:
            names = _ordered_unique(
                [*selected_skills, *self.skills.extract_mentions(message)]
            )
            for name in names:
                try:
                    results = await self.skills.inject_selected(
                        self.store, session_id, [name]
                    )
                except SkillNotFoundError as exc:
                    yield _error("skill_not_found", str(exc), run_id, recoverable=True)
                    continue
                result = results[0]
                yield AgentEvent(
                    "skill_loaded",
                    {
                        "name": result.name,
                        "status": "loaded" if result.loaded else "already_loaded",
                        "source": "explicit",
                        "request_id": run_id,
                    },
                )

            _raise_if_cancelled(active.cancel)
            await self.store.add_message(
                session_id,
                ChatMessage(role="user", content=message, metadata={"request_id": run_id}),
            )

            consecutive_failures = 0
            force_final = False
            for turn in range(1, self.config.max_turns + 1):
                turns = turn
                _raise_if_cancelled(active.cancel)
                prepared = await self.prepare_context(
                    session_id, workspace_id=workspace_id
                )
                schemas = None if force_final else self.tools.schemas()
                completed: _CompletedModelTurn | None = None
                async for item in self._model_turn(
                    self.clients.get_client("main", workspace_id),
                    prepared.messages,
                    schemas,
                    active.cancel,
                    turn,
                    run_id,
                ):
                    if isinstance(item, AgentEvent):
                        yield item
                    else:
                        completed = item
                if completed is None:
                    raise RuntimeError("LLM stream ended without a completed turn")

                content, calls = completed.content, completed.calls
                if content or calls:
                    await self.store.add_message(
                        session_id,
                        ChatMessage(
                            role="assistant",
                            content=content or None,
                            tool_calls=calls,
                            metadata={"request_id": run_id, "turn": turn},
                        ),
                    )

                if force_final:
                    if not content or calls:
                        warning = (
                            "I could not continue because the configured tool retry "
                            "limit was reached."
                        )
                        await self.store.add_message(
                            session_id,
                            ChatMessage(
                                role="assistant",
                                content=warning,
                                metadata={"kind": "agent_limit", "request_id": run_id},
                            ),
                        )
                        yield AgentEvent(
                            "text_delta",
                            {"delta": warning, "turn": turn, "request_id": run_id},
                        )
                    yield _done("tool_retry_limit", run_id, turns=turn)
                    return

                if not calls:
                    if not content:
                        yield _error(
                            "empty_model_response",
                            "The model returned neither text nor a tool call.",
                            run_id,
                        )
                        yield _done("error", run_id, turns=turn)
                    else:
                        yield _done("complete", run_id, turns=turn)
                    return

                for call in calls:
                    _raise_if_cancelled(active.cancel)
                    yield AgentEvent(
                        "tool_call",
                        {
                            "call_id": call.id,
                            "tool_call_id": call.id,
                            "name": call.name,
                            "arguments": dict(call.arguments),
                            "turn": turn,
                            "request_id": run_id,
                        },
                    )
                    context = ToolExecutionContext(
                        session_id=session_id,
                        store=self.store,
                        workspace_root=self._workspace(workspace_id),
                        request_id=run_id,
                        workspace_id=workspace_id,
                    )
                    result = await _await_or_cancel(
                        self.tools.execute(
                            call.name,
                            call.arguments,
                            context,
                            timeout_s=self.config.tool_timeout_s,
                            max_result_chars=self.config.tool_result_max_chars,
                        ),
                        active.cancel,
                    )
                    result, skill_event = await self._persist_tool_result(
                        session_id, call, result, run_id
                    )
                    yield AgentEvent(
                        "tool_result",
                        {
                            **result.event_data(tool_call_id=call.id),
                            "turn": turn,
                            "request_id": run_id,
                        },
                    )
                    if skill_event is not None:
                        yield skill_event
                    consecutive_failures = consecutive_failures + 1 if result.error else 0

                if consecutive_failures > self.config.max_tool_retries:
                    force_final = True
                    await self.store.add_message(
                        session_id,
                        ChatMessage(
                            role="user",
                            content=(
                                "<agent_control>The tool retry limit has been reached. "
                                "Do not call more tools; explain the limitation and give the "
                                "best answer possible from current context.</agent_control>"
                            ),
                            metadata={"kind": "agent_control", "request_id": run_id},
                        ),
                    )

                if turn == self.config.max_turns:
                    text = (
                        f"I stopped after reaching the configured limit of "
                        f"{self.config.max_turns} agent turns."
                    )
                    await self.store.add_message(
                        session_id,
                        ChatMessage(
                            role="assistant",
                            content=text,
                            metadata={"kind": "agent_limit", "request_id": run_id},
                        ),
                    )
                    yield AgentEvent(
                        "text_delta",
                        {"delta": text, "turn": turn, "request_id": run_id},
                    )
                    yield _error("max_turns_reached", text, run_id)
                    yield _done("max_turns", run_id, turns=turn)
                    return
        except AgentRunCancelled:
            yield _done("aborted", run_id, turns=turns)
        except asyncio.CancelledError:
            # Preserve normal cancellation semantics during server shutdown.
            raise
        except Exception as exc:
            yield _error("agent_error", str(exc) or exc.__class__.__name__, run_id)
            yield _done("error", run_id, turns=turns)
        finally:
            if self._active.get(session_id) is active:
                self._active.pop(session_id, None)

    async def _model_turn(
        self,
        client: LLMClient,
        messages: Sequence[ChatMessage],
        tools: Sequence[dict[str, Any]] | None,
        cancel: asyncio.Event,
        turn: int,
        request_id: str,
    ) -> AsyncIterator[AgentEvent | _CompletedModelTurn]:
        content_parts: list[str] = []
        buffers: dict[int, _ToolCallBuffer] = {}
        stream = client.stream(messages, tools=tools)
        if inspect.isawaitable(stream):
            stream = await _await_or_cancel(stream, cancel)
        iterator = stream.__aiter__()
        while True:
            try:
                chunk = await _next_or_cancel(iterator, cancel)
            except StopAsyncIteration:
                break
            if not isinstance(chunk, LLMStreamChunk):
                raise TypeError("LLM stream must yield LLMStreamChunk values")
            if chunk.content_delta:
                content_parts.append(chunk.content_delta)
                yield AgentEvent(
                    "text_delta",
                    {
                        "delta": chunk.content_delta,
                        "turn": turn,
                        "request_id": request_id,
                    },
                )
            for delta in chunk.tool_call_deltas:
                buffer = buffers.setdefault(delta.index, _ToolCallBuffer())
                if delta.id:
                    buffer.id = delta.id
                if delta.name:
                    buffer.name += delta.name
                if delta.arguments_delta:
                    buffer.arguments += delta.arguments_delta
        calls = tuple(
            ToolCall(
                id=buffer.id or f"call_{turn}_{index}",
                name=buffer.name,
                arguments=_parse_arguments(buffer.arguments),
            )
            for index, buffer in sorted(buffers.items())
        )
        yield _CompletedModelTurn("".join(content_parts), calls)

    async def _persist_tool_result(
        self,
        session_id: str,
        call: ToolCall,
        result: ToolExecutionResult,
        request_id: str,
    ) -> tuple[ToolExecutionResult, AgentEvent | None]:
        action = result.metadata.get("skill_action")
        skill_name = str(result.metadata.get("skill_name", ""))
        base_metadata: dict[str, Any] = {
            "kind": "tool_result",
            "request_id": request_id,
            "error": result.error,
            "truncated": result.truncated,
        }
        skill_event: AgentEvent | None = None

        if action == "load" and skill_name:
            skill_message = ChatMessage(
                role="tool",
                content=result.content,
                tool_call_id=call.id,
                name=call.name,
                metadata={
                    **base_metadata,
                    "kind": "skill_injection",
                    "skill_name": skill_name,
                },
            )
            inserted = await self.store.inject_skill(
                session_id, skill_name, skill_message
            )
            if inserted:
                skill_event = AgentEvent(
                    "skill_loaded",
                    {
                        "name": skill_name,
                        "status": "loaded",
                        "source": "tool",
                        "request_id": request_id,
                    },
                )
                return result, skill_event
            result = replace(
                result,
                content=json.dumps(
                    {"status": "already_loaded", "name": skill_name},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                metadata={"skill_action": "noop", "skill_name": skill_name},
            )
            skill_event = AgentEvent(
                "skill_loaded",
                {
                    "name": skill_name,
                    "status": "already_loaded",
                    "source": "tool",
                    "request_id": request_id,
                },
            )
        elif action == "remove" and skill_name:
            removed = await self.store.remove_session_skill(session_id, skill_name)
            result = replace(
                result,
                content=json.dumps(
                    {
                        "status": "removed" if removed else "not_loaded",
                        "name": skill_name,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            )

        await self.store.add_message(
            session_id,
            ChatMessage(
                role="tool",
                content=result.content,
                tool_call_id=call.id,
                name=call.name,
                metadata=base_metadata,
            ),
        )
        return result, skill_event

    def _workspace(self, workspace_id: str | None) -> Path:
        if self.workspace_resolver is not None:
            return Path(self.workspace_resolver(workspace_id)).resolve()
        if not workspace_id:
            return self.workspace_root
        root = (self.workspace_root / workspace_id).resolve()
        try:
            root.relative_to(self.workspace_root)
        except ValueError as exc:
            raise ValueError("workspace_id resolves outside the workspace root") from exc
        return root


async def _next_or_cancel(
    iterator: AsyncIterator[LLMStreamChunk], cancel: asyncio.Event
) -> LLMStreamChunk:
    next_task = asyncio.create_task(anext(iterator))
    cancel_task = asyncio.create_task(cancel.wait())
    done, _pending = await asyncio.wait(
        {next_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if cancel_task in done and cancel.is_set():
        next_task.cancel()
        with suppress(asyncio.CancelledError, StopAsyncIteration):
            await next_task
        raise AgentRunCancelled
    cancel_task.cancel()
    with suppress(asyncio.CancelledError):
        await cancel_task
    return await next_task


async def _await_or_cancel(value: Any, cancel: asyncio.Event) -> Any:
    value_task = asyncio.ensure_future(value)
    cancel_task = asyncio.create_task(cancel.wait())
    done, _pending = await asyncio.wait(
        {value_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
    )
    if cancel_task in done and cancel.is_set():
        value_task.cancel()
        with suppress(asyncio.CancelledError):
            await value_task
        raise AgentRunCancelled
    cancel_task.cancel()
    with suppress(asyncio.CancelledError):
        await cancel_task
    return await value_task


def _raise_if_cancelled(cancel: asyncio.Event) -> None:
    if cancel.is_set():
        raise AgentRunCancelled


def _parse_arguments(raw: str) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"__raw_arguments__": raw}
    if not isinstance(value, dict):
        return {"__raw_arguments__": raw}
    return value


def _ordered_unique(values: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _error(
    code: str,
    message: str,
    request_id: str,
    *,
    recoverable: bool = False,
) -> AgentEvent:
    return AgentEvent(
        "error",
        {
            "code": code,
            "message": message,
            "recoverable": recoverable,
            "request_id": request_id,
        },
    )


def _done(reason: str, request_id: str, *, turns: int) -> AgentEvent:
    return AgentEvent(
        "done", {"reason": reason, "turns": turns, "request_id": request_id}
    )

