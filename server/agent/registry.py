"""Tool contracts, registry, validation, and guarded execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .models import ImageAttachment
from .ports import ConversationStore


ToolExecutor = Callable[[Mapping[str, Any], "ToolExecutionContext"], Awaitable[Any]]
_TOOL_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


@dataclass(frozen=True, slots=True)
class ToolExecutionContext:
    session_id: str
    store: ConversationStore
    workspace_root: Path
    request_id: str
    workspace_id: str | None = None
    cancel_event: asyncio.Event | None = None


@dataclass(frozen=True, slots=True)
class ToolOutput:
    """Optional rich return value for executors that need core directives."""

    value: Any
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attachments: tuple[ImageAttachment, ...] = ()


@dataclass(frozen=True, slots=True)
class ToolExecutionResult:
    name: str
    content: str
    error: bool = False
    truncated: bool = False
    original_chars: int | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    attachments: tuple[ImageAttachment, ...] = ()

    def event_data(self, *, tool_call_id: str) -> dict[str, Any]:
        event = {
            "call_id": tool_call_id,
            # Compatibility alias for adapters that mirror the persisted field.
            "tool_call_id": tool_call_id,
            "name": self.name,
            "content": self.content,
            "error": self.error,
            "truncated": self.truncated,
            **(
                {"original_chars": self.original_chars}
                if self.original_chars is not None
                else {}
            ),
        }
        patch = self.metadata.get("ui_patch")
        if isinstance(patch, str):
            event["patch"] = patch
            event["patch_truncated"] = bool(
                self.metadata.get("ui_patch_truncated", False)
            )
        try:
            event["result"] = json.loads(self.content)
        except (TypeError, json.JSONDecodeError):
            event["result"] = self.content
        return event


@dataclass(frozen=True, slots=True)
class Tool:
    """Uniform tool definition exposed to both the registry and the LLM."""

    name: str
    description: str
    parameters: Mapping[str, Any]
    executor: ToolExecutor

    def __post_init__(self) -> None:
        if not _TOOL_NAME.fullmatch(self.name):
            raise ValueError(f"Invalid tool name: {self.name!r}")
        if not self.description.strip():
            raise ValueError("Tool description cannot be empty")
        if self.parameters.get("type") != "object":
            raise ValueError("Tool parameters must use a JSON Schema object root")
        if not callable(self.executor):
            raise TypeError("Tool executor must be callable")

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": dict(self.parameters),
            },
        }


class ToolRegistry:
    def __init__(self, tools: Sequence[Tool] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: Tool, *, replace: bool = False) -> Tool:
        if tool.name in self._tools and not replace:
            raise ValueError(f"Tool already registered: {tool.name}")
        self._tools[tool.name] = tool
        return tool

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list(self) -> tuple[Tool, ...]:
        return tuple(self._tools.values())

    def schemas(self) -> list[dict[str, Any]]:
        return [tool.schema() for tool in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
        *,
        timeout_s: float,
        max_result_chars: int,
    ) -> ToolExecutionResult:
        tool = self.get(name)
        if tool is None:
            return _error_result(name, f"Unknown tool: {name}")
        validation_error = _validate_arguments(arguments, tool.parameters)
        if validation_error:
            return _error_result(name, f"Invalid arguments: {validation_error}")
        try:
            result = tool.executor(arguments, context)
            if not inspect.isawaitable(result):
                raise TypeError("Tool executor must return an awaitable")
            value = await asyncio.wait_for(result, timeout=timeout_s)
            metadata: Mapping[str, Any] = {}
            attachments: tuple[ImageAttachment, ...] = ()
            if isinstance(value, ToolOutput):
                metadata = value.metadata
                attachments = value.attachments
                value = value.value
            content = _serialise(value)
            is_error = isinstance(value, Mapping) and value.get("error") is True
            return _truncate_result(
                ToolExecutionResult(
                    name=name,
                    content=content,
                    error=is_error,
                    metadata=metadata,
                    attachments=attachments,
                ),
                max_result_chars,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            return _error_result(name, f"Tool timed out after {timeout_s:g}s")
        except Exception as exc:
            return _error_result(name, str(exc) or exc.__class__.__name__)


def _serialise(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def _error_result(name: str, message: str) -> ToolExecutionResult:
    return ToolExecutionResult(
        name=name,
        content=json.dumps(
            {"error": True, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        error=True,
    )


def _truncate_result(
    result: ToolExecutionResult, max_result_chars: int
) -> ToolExecutionResult:
    if max_result_chars < 64:
        raise ValueError("max_result_chars must be at least 64")
    if len(result.content) <= max_result_chars:
        return result
    marker = f"\n...[truncated; original_chars={len(result.content)}]"
    prefix_size = max(0, max_result_chars - len(marker))
    return ToolExecutionResult(
        name=result.name,
        content=result.content[:prefix_size] + marker,
        error=result.error,
        truncated=True,
        original_chars=len(result.content),
        metadata=result.metadata,
        attachments=result.attachments,
    )


def _validate_arguments(arguments: Mapping[str, Any], schema: Mapping[str, Any]) -> str | None:
    if not isinstance(arguments, Mapping):
        return "arguments must be an object"
    if "__raw_arguments__" in arguments:
        return "model returned malformed JSON arguments"
    required = schema.get("required", ())
    for key in required:
        if key not in arguments:
            return f"missing required property '{key}'"
    properties = schema.get("properties", {})
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            return f"unknown property '{unknown[0]}'"
    for key, value in arguments.items():
        prop = properties.get(key)
        if not isinstance(prop, Mapping):
            continue
        expected = prop.get("type")
        if expected and not _matches_json_type(value, expected):
            return f"property '{key}' must be {expected}"
        if "enum" in prop and value not in prop["enum"]:
            return f"property '{key}' must be one of {list(prop['enum'])}"
    return None


def _matches_json_type(value: Any, expected: str | Sequence[str]) -> bool:
    if isinstance(expected, Sequence) and not isinstance(expected, str):
        return any(_matches_json_type(value, item) for item in expected)
    checks: dict[str, Callable[[Any], bool]] = {
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float))
        and not isinstance(item, bool)
        and math.isfinite(item),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, Mapping),
        "array": lambda item: isinstance(item, list),
        "null": lambda item: item is None,
    }
    return checks.get(str(expected), lambda _item: True)(value)
