"""Small HTTP-layer adapters with no persistence dependencies."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
from inspect import isawaitable
from pathlib import Path
from typing import Any, Callable
import asyncio
import uuid

from server.config import AppConfig
from server.services.document_exporter import DocumentExporter


EVENT_TYPES = frozenset(
    {
        "text_delta",
        "reasoning_delta",
        "tool_call",
        "tool_result",
        "skill_loaded",
        "conversation_state",
        "error",
        "done",
    }
)


class AgentAdapter:
    """Normalize a request-scoped AgentCore for the SSE transport."""

    def __init__(self, agent: Any):
        self.agent = agent

    async def stream(
        self,
        *,
        session_id: str,
        message: str,
        selected_skills: Sequence[str],
        workspace_id: str,
        request_id: str,
        reasoning_effort: str,
    ) -> AsyncIterator[dict[str, Any]]:
        result = self.agent.stream(
            session_id=session_id,
            message=message,
            selected_skills=tuple(selected_skills),
            workspace_id=workspace_id,
            request_id=request_id,
            reasoning_effort=reasoning_effort,
        )
        if isawaitable(result):
            result = await result
        async for event in result:
            yield normalize_event(event)

    async def abort(self, session_id: str, request_id: str | None = None) -> bool:
        result = self.agent.abort(session_id, request_id)
        if isawaitable(result):
            result = await result
        return bool(result)


def normalize_event(event: Any) -> dict[str, Any]:
    if hasattr(event, "to_dict"):
        event = event.to_dict()
    if isinstance(event, str):
        return {"type": "text_delta", "delta": event}
    if not isinstance(event, Mapping):
        raise TypeError("invalid_agent_event")
    normalized = dict(event)
    event_type = str(normalized.get("type", ""))
    if event_type not in EVENT_TYPES:
        raise ValueError("invalid_agent_event")
    normalized["type"] = event_type
    if event_type in {"tool_call", "tool_result"} and "call_id" not in normalized:
        if "tool_call_id" in normalized:
            normalized["call_id"] = normalized["tool_call_id"]
    if event_type == "skill_loaded" and "already_loaded" not in normalized:
        normalized["already_loaded"] = normalized.get("status") == "already_loaded"
    return normalized


class SkillCatalogAdapter:
    def __init__(self, manager: Any):
        self.manager = manager

    def list_public(self) -> list[dict[str, str]]:
        values = self.manager.catalog() if hasattr(self.manager, "catalog") else self.manager.list()
        result: list[dict[str, str]] = []
        for value in values:
            value = value.public_dict() if hasattr(value, "public_dict") else value
            if isinstance(value, Mapping) and value.get("name"):
                result.append(
                    {
                        "name": str(value["name"]),
                        "description": str(value.get("description", "")),
                    }
                )
        return result


@dataclass(slots=True)
class ActiveRun:
    request_id: str
    abort_event: asyncio.Event = field(default_factory=asyncio.Event)
    abort_handler: Any | None = None


class RunCoordinator:
    """Only tracks currently executing request objects in process memory."""

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

    async def bind_abort_handler(
        self,
        workspace_id: str,
        session_id: str,
        request_id: str,
        handler: Any,
    ) -> bool:
        async with self._lock:
            run = self._active.get((workspace_id, session_id))
            if run is None or run.request_id != request_id:
                return False
            run.abort_handler = handler
            return True

    async def finish(self, workspace_id: str, session_id: str, request_id: str) -> None:
        async with self._lock:
            current = self._active.get((workspace_id, session_id))
            if current is not None and current.request_id == request_id:
                self._active.pop((workspace_id, session_id), None)

    async def request_abort(
        self, workspace_id: str, session_id: str, request_id: str | None = None
    ) -> bool:
        handler: Any | None = None
        async with self._lock:
            run = self._active.get((workspace_id, session_id))
            if run is None or (request_id is not None and run.request_id != request_id):
                return False
            run.abort_event.set()
            handler = run.abort_handler
        if handler is not None:
            try:
                result = handler()
                if isawaitable(result):
                    await result
            except Exception:
                pass
        return True

    @property
    def active_count(self) -> int:
        return len(self._active)


@dataclass(slots=True)
class APIServices:
    config: AppConfig
    skill_catalog: SkillCatalogAdapter
    runtime_factory: Any
    runs: RunCoordinator = field(default_factory=RunCoordinator)
    document_exporter: DocumentExporter | None = None
    workspace_resolver: Callable[[str | None], Path] | None = None


__all__ = [
    "APIServices",
    "AgentAdapter",
    "EVENT_TYPES",
    "RunCoordinator",
    "SkillCatalogAdapter",
    "normalize_event",
]
