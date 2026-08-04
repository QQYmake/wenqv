"""Framework-free value objects shared by the agent core and its adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Mapping


MessageRole = Literal["system", "user", "assistant", "tool"]
EventType = Literal[
    "text_delta",
    "tool_call",
    "tool_result",
    "skill_loaded",
    "error",
    "done",
]


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A complete function/tool call requested by an LLM."""

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)

    def to_openai(self) -> dict[str, Any]:
        import json

        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(
                    dict(self.arguments), ensure_ascii=False, separators=(",", ":")
                ),
            },
        }


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """A provider-neutral chat message.

    ``metadata`` is persisted for bookkeeping but is deliberately excluded from
    the payload sent to an OpenAI-compatible provider.
    """

    role: MessageRole
    content: str | None = None
    tool_calls: tuple[ToolCall, ...] = ()
    tool_call_id: str | None = None
    name: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def to_llm_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"role": self.role}
        if self.content is not None:
            result["content"] = self.content
        elif self.role == "assistant":
            result["content"] = None
        if self.tool_calls:
            result["tool_calls"] = [call.to_openai() for call in self.tool_calls]
        if self.tool_call_id is not None:
            result["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            result["name"] = self.name
        return result

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [
                {"id": c.id, "name": c.name, "arguments": dict(c.arguments)}
                for c in self.tool_calls
            ],
            "tool_call_id": self.tool_call_id,
            "name": self.name,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "ChatMessage":
        calls = tuple(
            ToolCall(
                id=str(call["id"]),
                name=str(call["name"]),
                arguments=dict(call.get("arguments") or {}),
            )
            for call in value.get("tool_calls", ())
        )
        return cls(
            role=value["role"],
            content=value.get("content"),
            tool_calls=calls,
            tool_call_id=value.get("tool_call_id"),
            name=value.get("name"),
            metadata=dict(value.get("metadata") or {}),
        )


@dataclass(frozen=True, slots=True)
class AgentEvent:
    """One transport-neutral event produced by :class:`AgentCore`."""

    type: EventType
    data: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, **dict(self.data)}


@dataclass(frozen=True, slots=True)
class ToolCallDelta:
    """A partial tool call from a streaming model response."""

    index: int
    id: str | None = None
    name: str | None = None
    arguments_delta: str = ""


@dataclass(frozen=True, slots=True)
class LLMStreamChunk:
    """Provider-neutral streaming response chunk."""

    content_delta: str = ""
    tool_call_deltas: tuple[ToolCallDelta, ...] = ()
    finish_reason: str | None = None


@dataclass(frozen=True, slots=True)
class LLMResponse:
    """Provider-neutral non-streaming response."""

    content: str
    tool_calls: tuple[ToolCall, ...] = ()
    finish_reason: str | None = None

