"""Validated browser-to-server request models.

These are transport boundaries, not persistence models: accepted state exists
only while a request is executing and the browser remains the canonical owner.
"""

from __future__ import annotations

from typing import Any, Literal
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


MAX_RUNTIME_BYTES = 4 * 1024 * 1024
MAX_CHAT_REQUEST_BYTES = 5 * 1024 * 1024


class ProviderConfigBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=4096)
    model: str = Field(min_length=1, max_length=256)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_s: float | None = Field(default=None, ge=1, le=3600)

    @field_validator("base_url", "model")
    @classmethod
    def strip_nonempty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("value cannot be blank")
        return value


class OptionalProviderConfigBody(BaseModel):
    """An all-empty summary config means reuse the main provider."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="", max_length=512)
    api_key: str = Field(default="", max_length=4096)
    model: str = Field(default="", max_length=256)
    max_tokens: int | None = Field(default=None, ge=1, le=1_000_000)
    temperature: float | None = Field(default=None, ge=0, le=2)
    timeout_s: float | None = Field(default=None, ge=1, le=3600)

    @model_validator(mode="after")
    def complete_or_empty(self) -> "OptionalProviderConfigBody":
        fields = (self.base_url.strip(), self.api_key, self.model.strip())
        if any(fields) and not all(fields):
            raise ValueError("summary provider must be complete or empty")
        self.base_url = fields[0]
        self.model = fields[2]
        return self


class ProviderConfigSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    main: ProviderConfigBody
    summary: OptionalProviderConfigBody = Field(default_factory=OptionalProviderConfigBody)


class ToolCallState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=256)
    name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class RuntimeMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Literal["system", "user", "assistant", "tool"]
    content: str | None = Field(default=None, max_length=1_000_000)
    tool_calls: list[ToolCallState] = Field(default_factory=list, max_length=128)
    tool_call_id: str | None = Field(default=None, max_length=256)
    name: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class RuntimeContextBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    messages: list[RuntimeMessage] = Field(default_factory=list, max_length=2_000)
    active_skills: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("active_skills")
    @classmethod
    def normalize_skills(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            name = raw.strip()
            if name and name not in result:
                result.append(name)
        return result


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000_000)
    runtime_context: RuntimeContextBody = Field(default_factory=RuntimeContextBody)
    provider_config: ProviderConfigSet
    skills: list[str] = Field(default_factory=list, max_length=64)
    request_id: str | None = Field(default=None, max_length=128)
    reasoning_effort: Literal["low", "medium", "high", "max"] = "medium"

    @field_validator("session_id", "message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value

    @field_validator("skills")
    @classmethod
    def normalize_requested_skills(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        for raw in value:
            name = raw.strip()
            if name and name not in result:
                result.append(name)
        return result

    @model_validator(mode="after")
    def limit_total_runtime_size(self) -> "ChatRequest":
        # Validate encoded rather than character length: a JSON object full of
        # nested tool output can otherwise evade individual text field limits.
        try:
            encoded = json.dumps(
                self.runtime_context.model_dump(mode="json"),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError):
            raise ValueError("runtime context is not JSON-safe") from None
        if len(encoded) > MAX_RUNTIME_BYTES:
            raise ValueError("runtime context exceeds maximum size")
        whole_request = json.dumps(
            self.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        if len(whole_request) > MAX_CHAT_REQUEST_BYTES:
            raise ValueError("chat request exceeds maximum size")
        return self


class AbortChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)


class ProviderTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider_config: ProviderConfigSet


class ModelDiscoveryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1, max_length=512)
    api_key: str = Field(min_length=1, max_length=4096)

    @field_validator("base_url")
    @classmethod
    def normalize_base_url(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("base_url cannot be blank")
        return value


__all__ = [
    "AbortChatRequest",
    "ChatRequest",
    "MAX_RUNTIME_BYTES",
    "MAX_CHAT_REQUEST_BYTES",
    "ModelDiscoveryRequest",
    "OptionalProviderConfigBody",
    "ProviderConfigBody",
    "ProviderConfigSet",
    "ProviderTestRequest",
    "RuntimeContextBody",
]
