"""Validated HTTP request models."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class CreateSessionRequest(BaseModel):
    title: str = Field(default="New conversation", max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        return value.strip() or "New conversation"


class RenameSessionRequest(BaseModel):
    title: str = Field(min_length=1, max_length=160)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title cannot be blank")
        return value


class ChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1_000_000)
    skills: list[str] = Field(default_factory=list, max_length=64)
    request_id: str | None = Field(default=None, max_length=128)

    @field_validator("session_id", "message")
    @classmethod
    def reject_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value cannot be blank")
        return value

    @field_validator("skills")
    @classmethod
    def normalize_skills(cls, value: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_name in value:
            name = raw_name.strip()
            if name and name not in seen:
                seen.add(name)
                result.append(name)
        return result


class AbortChatRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=128)
    request_id: str | None = Field(default=None, max_length=128)


__all__ = [
    "AbortChatRequest",
    "ChatRequest",
    "CreateSessionRequest",
    "RenameSessionRequest",
]
