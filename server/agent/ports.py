"""Ports used by the framework-independent agent application service."""

from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Protocol, Sequence, runtime_checkable

from .models import ChatMessage, LLMResponse, LLMStreamChunk


@runtime_checkable
class LLMClient(Protocol):
    """Driven port for one OpenAI-compatible chat model."""

    def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict] | None = None,
        max_tokens: int | None = None,
    ) -> AsyncIterator[LLMStreamChunk]: ...

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse: ...


@runtime_checkable
class LLMClientProvider(Protocol):
    """Selects a model by role while hiding summary-to-main fallback."""

    def get_client(self, role: str) -> LLMClient: ...


@runtime_checkable
class ConversationStore(Protocol):
    """Persistence contract required by the agent core.

    ``inject_skill`` must atomically check the active skill set, persist the
    injection message, and return ``False`` when the skill was already active.
    ``remove_session_skill`` must make old skill instructions unavailable to
    future context reads. For tool-role injections an adapter may redact the
    old body instead of deleting the message, preserving tool-call ordering.
    """

    async def list_messages(self, session_id: str) -> Sequence[ChatMessage]: ...

    async def add_message(self, session_id: str, message: ChatMessage) -> None: ...

    async def replace_messages(
        self, session_id: str, messages: Sequence[ChatMessage]
    ) -> None: ...

    async def list_session_skills(self, session_id: str) -> set[str]: ...

    async def inject_skill(
        self, session_id: str, skill_name: str, message: ChatMessage
    ) -> bool: ...

    async def remove_session_skill(self, session_id: str, skill_name: str) -> bool: ...


class WorkspaceResolver(Protocol):
    def __call__(self, workspace_id: str | None) -> Path: ...

