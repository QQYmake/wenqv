"""Application-service ports and compatibility adapters for the HTTP layer."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from inspect import isawaitable
from typing import Any
import asyncio
import uuid

from server.config import AppConfig
from server.storage import AgentStoreAdapter, SQLiteStore


EVENT_TYPES = frozenset(
    {"text_delta", "tool_call", "tool_result", "skill_loaded", "error", "done"}
)


class UnavailableAgent:
    """Safe startup fallback used when Agent Core composition fails."""

    persists_messages = False

    def __init__(self, reason: str = "Agent Core is not configured"):
        self.reason = reason

    async def stream(self, **_: Any) -> AsyncIterator[dict[str, Any]]:
        yield {
            "type": "error",
            "code": "agent_unavailable",
            "message": self.reason,
            "recoverable": False,
        }
        yield {"type": "done", "finish_reason": "error"}

    async def abort(self, *_: Any, **__: Any) -> bool:
        return False


class AgentAdapter:
    """Normalize Agent Core methods and events for the API transport."""

    def __init__(self, agent: Any):
        self.agent = agent
        self.persists_messages = bool(getattr(agent, "persists_messages", True))

    async def stream(
        self,
        *,
        session_id: str,
        message: str,
        selected_skills: Sequence[str],
        workspace_id: str,
        request_id: str,
    ) -> AsyncIterator[dict[str, Any]]:
        method = (
            getattr(self.agent, "stream", None)
            or getattr(self.agent, "run_stream", None)
            or getattr(self.agent, "stream_chat", None)
            or getattr(self.agent, "run", None)
        )
        if method is None:
            raise RuntimeError("Agent does not expose stream/run_stream/stream_chat/run")
        try:
            result = method(
                session_id=session_id,
                message=message,
                selected_skills=tuple(selected_skills),
                workspace_id=workspace_id,
                request_id=request_id,
            )
        except TypeError as first_error:
            try:
                result = method(
                    session_id=session_id,
                    message=message,
                    skills=tuple(selected_skills),
                    request_id=request_id,
                )
            except TypeError:
                raise first_error
        if isawaitable(result):
            result = await result
        if hasattr(result, "__aiter__"):
            async for event in result:
                yield normalize_event(event)
            return
        if isinstance(result, str):
            yield {"type": "text_delta", "delta": result}
            return
        if isinstance(result, Mapping) or hasattr(result, "to_dict"):
            yield normalize_event(result)
            return
        if result is not None:
            for event in result:
                yield normalize_event(event)

    async def abort(self, session_id: str, request_id: str | None = None) -> bool:
        method = getattr(self.agent, "abort", None) or getattr(self.agent, "cancel", None)
        if method is None:
            return False
        try:
            result = method(session_id=session_id, request_id=request_id)
        except TypeError:
            result = method(session_id)
        if isawaitable(result):
            result = await result
        return bool(result)


def normalize_event(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    elif hasattr(event, "type") and hasattr(event, "data"):
        event = {"type": event.type, **dict(event.data)}
    if isinstance(event, str):
        return {"type": "text_delta", "delta": event}
    if not isinstance(event, Mapping):
        raise TypeError(f"Unsupported Agent event: {type(event).__name__}")
    normalized = dict(event)
    event_type = str(normalized.get("type", ""))
    if event_type not in EVENT_TYPES:
        raise ValueError(f"Unsupported Agent event type: {event_type or '<missing>'}")
    normalized["type"] = event_type
    if event_type in {"tool_call", "tool_result"}:
        if "call_id" not in normalized and "tool_call_id" in normalized:
            normalized["call_id"] = normalized["tool_call_id"]
    if event_type == "tool_call" and "arguments" not in normalized:
        if "args" in normalized:
            normalized["arguments"] = normalized["args"]
    if event_type == "tool_result" and "result" not in normalized:
        if "content" in normalized:
            normalized["result"] = normalized["content"]
    if event_type == "skill_loaded" and "already_loaded" not in normalized:
        normalized["already_loaded"] = normalized.get("status") == "already_loaded"
    if event_type == "done" and "finish_reason" not in normalized:
        if "reason" in normalized:
            normalized["finish_reason"] = normalized["reason"]
    return normalized


class SkillCatalogAdapter:
    def __init__(self, manager: Any | None):
        self.manager = manager

    def list_public(self) -> list[dict[str, str]]:
        if self.manager is None:
            return []
        if hasattr(self.manager, "catalog"):
            values = self.manager.catalog()
        else:
            values = self.manager.list()
        result: list[dict[str, str]] = []
        for value in values:
            if hasattr(value, "public_dict"):
                value = value.public_dict()
            if isinstance(value, Mapping):
                result.append(
                    {
                        "name": str(value.get("name", "")),
                        "description": str(value.get("description", "")),
                    }
                )
            else:
                result.append(
                    {
                        "name": str(getattr(value, "name", "")),
                        "description": str(getattr(value, "description", "")),
                    }
                )
        return [value for value in result if value["name"]]


@dataclass(slots=True)
class ActiveRun:
    request_id: str
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)


class RunCoordinator:
    """Tracks one active run per workspace/session for abort and conflict control."""

    def __init__(self) -> None:
        self._active: dict[tuple[str, str], ActiveRun] = {}
        self._lock = asyncio.Lock()

    async def start(
        self, workspace_id: str, session_id: str, request_id: str | None = None
    ) -> ActiveRun | None:
        key = (workspace_id, session_id)
        async with self._lock:
            if key in self._active:
                return None
            run = ActiveRun(request_id=request_id or str(uuid.uuid4()))
            self._active[key] = run
            return run

    async def finish(self, workspace_id: str, session_id: str, request_id: str) -> None:
        key = (workspace_id, session_id)
        async with self._lock:
            current = self._active.get(key)
            if current is not None and current.request_id == request_id:
                self._active.pop(key, None)

    async def request_abort(
        self, workspace_id: str, session_id: str, request_id: str | None = None
    ) -> bool:
        async with self._lock:
            run = self._active.get((workspace_id, session_id))
            if run is None or (request_id is not None and run.request_id != request_id):
                return False
            run.abort_event.set()
            return True


@dataclass(slots=True)
class APIServices:
    config: AppConfig
    store: SQLiteStore
    agent: AgentAdapter
    skill_catalog: SkillCatalogAdapter
    agent_store: AgentStoreAdapter
    title_generator: Any | None = None
    runs: RunCoordinator = field(default_factory=RunCoordinator)


__all__ = [
    "APIServices",
    "AgentAdapter",
    "EVENT_TYPES",
    "RunCoordinator",
    "SkillCatalogAdapter",
    "UnavailableAgent",
    "normalize_event",
]
