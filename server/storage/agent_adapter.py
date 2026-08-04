"""Adapter from SQLite records to the Agent Core's conversation-store port."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from server.agent.models import ChatMessage, ToolCall

from .sqlite import SQLiteStore


class AgentStoreAdapter:
    """Present :class:`SQLiteStore` as a framework-free ConversationStore."""

    def __init__(self, store: SQLiteStore):
        self.store = store

    async def list_messages(self, session_id: str) -> Sequence[ChatMessage]:
        records = await self.store.list_messages(session_id)
        if records is None:
            return ()
        return tuple(_from_record(record) for record in records)

    async def add_message(self, session_id: str, message: ChatMessage) -> None:
        await self.store.add_message(
            session_id,
            message.role,
            message.content,
            kind=_kind_for(message),
            name=message.name,
            metadata=_metadata_for(message),
        )

    async def replace_messages(
        self, session_id: str, messages: Sequence[ChatMessage]
    ) -> None:
        await self.store.replace_all_messages(
            session_id,
            [
                {
                    "role": message.role,
                    "content": message.content,
                    "kind": _kind_for(message),
                    "name": message.name,
                    "metadata": _metadata_for(message),
                }
                for message in messages
            ],
        )

    async def list_session_skills(self, session_id: str) -> set[str]:
        records = await self.store.list_loaded_skills(session_id)
        return {record["skill_name"] for record in records or ()}

    async def inject_skill(
        self, session_id: str, skill_name: str, message: ChatMessage
    ) -> bool:
        _, existed = await self.store.inject_skill_message(
            session_id,
            skill_name,
            message.content or "",
            role=message.role,
            metadata=_metadata_for(message),
        )
        return not existed

    async def remove_session_skill(self, session_id: str, skill_name: str) -> bool:
        return await self.store.remove_skill(session_id, skill_name)


def _kind_for(message: ChatMessage) -> str:
    declared = message.metadata.get("kind")
    if declared == "skill_injection":
        return "skill"
    if declared in {"summary", "conversation_summary", "context_summary"}:
        return "summary"
    if message.role == "tool":
        return "tool_result"
    if message.tool_calls:
        return "tool_call"
    return "message"


def _metadata_for(message: ChatMessage) -> dict[str, Any]:
    metadata = dict(message.metadata)
    metadata["_agent"] = {
        "tool_calls": [
            {"id": call.id, "name": call.name, "arguments": dict(call.arguments)}
            for call in message.tool_calls
        ],
        "tool_call_id": message.tool_call_id,
    }
    return metadata


def _from_record(record: Mapping[str, Any]) -> ChatMessage:
    metadata = dict(record.get("metadata") or {})
    agent_data = metadata.pop("_agent", {})
    if not isinstance(agent_data, Mapping):
        agent_data = {}
    calls = tuple(
        ToolCall(
            id=str(call.get("id", "")),
            name=str(call.get("name", "")),
            arguments=dict(call.get("arguments") or {}),
        )
        for call in agent_data.get("tool_calls", ())
        if isinstance(call, Mapping)
    )
    return ChatMessage(
        role=record["role"],
        content=record.get("content"),
        tool_calls=calls,
        tool_call_id=agent_data.get("tool_call_id"),
        name=record.get("name"),
        metadata=metadata,
    )


__all__ = ["AgentStoreAdapter"]
