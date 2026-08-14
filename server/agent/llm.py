"""OpenAI-compatible LLM adapter with main/summary role selection."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Literal, cast

from .models import (
    ChatMessage,
    LLMResponse,
    LLMStreamChunk,
    ToolCall,
    ToolCallDelta,
)
from .ports import LLMClient


LLMRole = Literal["main", "summary"]


@dataclass(frozen=True, slots=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str
    max_tokens: int | None = None
    temperature: float | None = None
    timeout_s: float = 120.0
    extra_body: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "LLMConfig":
        known = {
            "base_url",
            "api_key",
            "model",
            "max_tokens",
            "temperature",
            "timeout_s",
            "extra_body",
        }
        missing = [key for key in ("base_url", "api_key", "model") if not value.get(key)]
        if missing:
            raise ValueError(f"Missing LLM configuration: {', '.join(missing)}")
        extra = dict(value.get("extra_body") or {})
        # Unknown provider-specific keys are sent in ``extra_body``.
        extra.update({key: val for key, val in value.items() if key not in known})
        return cls(
            base_url=str(value["base_url"]),
            api_key=str(value["api_key"]),
            model=str(value["model"]),
            max_tokens=(int(value["max_tokens"]) if value.get("max_tokens") else None),
            temperature=(
                float(value["temperature"]) if value.get("temperature") is not None else None
            ),
            timeout_s=float(value.get("timeout_s", 120.0)),
            extra_body=extra,
        )

    @classmethod
    def from_value(cls, value: Any) -> "LLMConfig":
        """Coerce mappings or application-level config objects at the edge."""

        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls.from_mapping(value)
        keys = (
            "base_url",
            "api_key",
            "model",
            "max_tokens",
            "temperature",
            "timeout_s",
            "extra_body",
        )
        mapped = {key: getattr(value, key) for key in keys if hasattr(value, key)}
        if not all(key in mapped for key in ("base_url", "api_key", "model")):
            raise TypeError(
                "LLM source must be a client, mapping, LLMConfig, or config-like object"
            )
        return cls.from_mapping(mapped)

    def __post_init__(self) -> None:
        if self.timeout_s <= 0:
            raise ValueError("LLM timeout_s must be positive")


def _arguments_from_json(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"__raw_arguments__": raw}
    if isinstance(parsed, dict):
        return parsed
    return {"__raw_arguments__": raw}


class OpenAICompatClient:
    """Thin adapter around ``openai.AsyncOpenAI``.

    The dependency is imported lazily so the domain and unit tests do not need
    the OpenAI package installed. A compatible SDK client may also be injected.
    """

    def __init__(self, config: LLMConfig, *, sdk_client: Any | None = None) -> None:
        self.config = config
        self._sdk_client = sdk_client
        self._vision_supported: bool | None = None

    def _client(self) -> Any:
        if self._sdk_client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - integration guard
                raise RuntimeError(
                    "The 'openai' package is required for OpenAICompatClient. "
                    "Activate the project virtualenv (.venv) and run: "
                    "python -m pip install -r requirements.txt, then restart the server."
                ) from exc
            self._sdk_client = AsyncOpenAI(
                base_url=self.config.base_url,
                api_key=self.config.api_key,
                timeout=self.config.timeout_s,
            )
        return self._sdk_client

    def _request(self, messages: Sequence[ChatMessage], **overrides: Any) -> dict[str, Any]:
        request: dict[str, Any] = {
            "model": self.config.model,
            "messages": [message.to_llm_dict() for message in messages],
        }
        tools = overrides.get("tools")
        if tools:
            request["tools"] = list(tools)
            request["tool_choice"] = "auto"
        max_tokens = overrides.get("max_tokens") or self.config.max_tokens
        if max_tokens is not None:
            request["max_tokens"] = max_tokens
        if self.config.temperature is not None:
            request["temperature"] = self.config.temperature
        reasoning_effort = overrides.get("reasoning_effort")
        if reasoning_effort is not None:
            request["reasoning_effort"] = reasoning_effort
        if self.config.extra_body:
            request["extra_body"] = dict(self.config.extra_body)
        return request

    async def complete(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict] | None = None,
        max_tokens: int | None = None,
    ) -> LLMResponse:
        response = await self._client().chat.completions.create(
            **self._request(messages, tools=tools, max_tokens=max_tokens),
            stream=False,
        )
        choice = response.choices[0]
        message = choice.message
        calls = tuple(
            ToolCall(
                id=str(call.id),
                name=str(call.function.name),
                arguments=_arguments_from_json(call.function.arguments),
            )
            for call in (message.tool_calls or ())
        )
        return LLMResponse(
            content=message.content or "",
            tool_calls=calls,
            finish_reason=getattr(choice, "finish_reason", None),
        )

    async def stream(
        self,
        messages: Sequence[ChatMessage],
        *,
        tools: Sequence[dict] | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str | None = None,
    ) -> AsyncIterator[LLMStreamChunk]:
        request_messages = (
            _without_images(messages)
            if self._vision_supported is False and _has_images(messages)
            else tuple(messages)
        )
        request = self._request(
            request_messages,
            tools=tools,
            max_tokens=max_tokens,
            reasoning_effort=reasoning_effort,
        )
        try:
            response = await self._client().chat.completions.create(
                **request, stream=True
            )
        except Exception as exc:
            if not _has_images(request_messages) or not _is_explicit_vision_rejection(exc):
                raise
            # Compatible gateways vary in multimodal support. Retry exactly once
            # without image parts only when the endpoint explicitly rejects them.
            self._vision_supported = False
            fallback_messages = _without_images(request_messages)
            response = await self._client().chat.completions.create(
                **self._request(
                    fallback_messages,
                    tools=tools,
                    max_tokens=max_tokens,
                    reasoning_effort=reasoning_effort,
                ),
                stream=True,
            )
        async for chunk in response:
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            delta = choice.delta
            tool_deltas = tuple(
                ToolCallDelta(
                    index=int(call.index),
                    id=getattr(call, "id", None),
                    name=(
                        getattr(getattr(call, "function", None), "name", None)
                        or None
                    ),
                    arguments_delta=(
                        getattr(getattr(call, "function", None), "arguments", None)
                        or ""
                    ),
                )
                for call in (getattr(delta, "tool_calls", None) or ())
            )
            yield LLMStreamChunk(
                content_delta=getattr(delta, "content", None) or "",
                reasoning_delta=_reasoning_text(delta),
                tool_call_deltas=tool_deltas,
                finish_reason=getattr(choice, "finish_reason", None),
            )


def _reasoning_text(delta: Any) -> str:
    """Extract plaintext summaries exposed by compatible Chat gateways.

    These fields are not an official Chat Completions summary contract. Only
    direct strings are forwarded; structured or opaque payloads are ignored.
    """

    for name in ("reasoning_content", "reasoning", "thinking"):
        value = getattr(delta, name, None)
        if isinstance(value, str):
            return value
    extra = getattr(delta, "model_extra", None)
    if isinstance(extra, Mapping):
        for name in ("reasoning_content", "reasoning", "thinking"):
            value = extra.get(name)
            if isinstance(value, str):
                return value
    return ""


def _has_images(messages: Sequence[ChatMessage]) -> bool:
    return any(message.attachments for message in messages)


def _without_images(messages: Sequence[ChatMessage]) -> tuple[ChatMessage, ...]:
    fallback: list[ChatMessage] = []
    for message in messages:
        if not message.attachments:
            fallback.append(message)
            continue
        paths = ", ".join(attachment.path for attachment in message.attachments)
        notice = (
            "[The configured model endpoint does not support visual input. "
            f"Images could not be attached: {paths}]"
        )
        content = f"{message.content}\n\n{notice}" if message.content else notice
        fallback.append(replace(message, content=content, attachments=()))
    return tuple(fallback)


def _is_explicit_vision_rejection(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status not in {400, 422}:
        return False
    body = getattr(exc, "body", None)
    text = " ".join((str(exc), str(body or ""))).lower()
    modality = any(
        term in text
        for term in ("image", "vision", "multimodal", "image_url", "content part")
    )
    rejection = any(
        term in text
        for term in ("unsupported", "not support", "invalid", "not allowed", "unknown")
    )
    return modality and rejection


ClientBuilder = Callable[[LLMConfig], LLMClient]
ClientSource = LLMConfig | Mapping[str, Any] | LLMClient


def _looks_like_client(source: object) -> bool:
    return callable(getattr(source, "stream", None)) and callable(
        getattr(source, "complete", None)
    )


class LLMClientFactory:
    """Role-based client provider; summary transparently falls back to main."""

    def __init__(
        self,
        main: ClientSource,
        summary: ClientSource | None = None,
        *,
        builder: ClientBuilder = OpenAICompatClient,
    ) -> None:
        self._builder = builder
        self._sources: dict[LLMRole, ClientSource] = {
            "main": main,
            "summary": summary if summary is not None else main,
        }
        self._clients: dict[int, LLMClient] = {}

    def get_client(self, role: str, workspace_id: str | None = None) -> LLMClient:
        if role not in ("main", "summary"):
            raise ValueError("LLM role must be 'main' or 'summary'")
        # The legacy factory is workspace-agnostic; it serves the global default
        # config. Per-workspace selection is the resolver adapter's job.
        source = self._sources[cast(LLMRole, role)]
        cache_key = id(source)
        if cache_key not in self._clients:
            if _looks_like_client(source):
                client = cast(LLMClient, source)
            else:
                config = LLMConfig.from_value(source)
                client = self._builder(config)
            self._clients[cache_key] = client
        return self._clients[cache_key]

    def has_config(self, workspace_id: str | None = None) -> bool:
        """The default factory always reports a configured client."""

        return True


_default_factory: LLMClientFactory | None = None


def configure_clients(
    main: ClientSource,
    summary: ClientSource | None = None,
    *,
    builder: ClientBuilder = OpenAICompatClient,
) -> LLMClientFactory:
    """Configure the optional process-wide provider used by ``get_client``."""

    global _default_factory
    _default_factory = LLMClientFactory(main, summary, builder=builder)
    return _default_factory


def get_client(role: LLMRole, workspace_id: str | None = None) -> LLMClient:
    """Return a configured role client, applying summary fallback internally."""

    if _default_factory is None:
        raise RuntimeError("LLM clients have not been configured")
    return _default_factory.get_client(role, workspace_id)
