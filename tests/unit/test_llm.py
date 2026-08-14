from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from server.agent.llm import (
    LLMClientFactory,
    LLMConfig,
    OpenAICompatClient,
    _reasoning_text,
)
from server.agent.models import ChatMessage, ImageAttachment, LLMResponse, LLMStreamChunk


class FakeClient:
    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("ok")

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        yield LLMStreamChunk(content_delta="ok")


def test_summary_role_falls_back_to_exact_main_client() -> None:
    main = FakeClient()
    factory = LLMClientFactory(main)

    assert factory.get_client("main") is main
    assert factory.get_client("summary") is main


def test_distinct_summary_role_and_invalid_role() -> None:
    main, summary = FakeClient(), FakeClient()
    factory = LLMClientFactory(main, summary)

    assert factory.get_client("summary") is summary
    with pytest.raises(ValueError, match="main.*summary"):
        factory.get_client("other")


def test_mapping_build_is_lazy_cached_and_timeout_is_not_extra_body() -> None:
    built: list[LLMConfig] = []

    def builder(config: LLMConfig) -> FakeClient:
        built.append(config)
        return FakeClient()

    factory = LLMClientFactory(
        {
            "base_url": "https://example.test/v1",
            "api_key": "secret",
            "model": "main-model",
            "timeout_s": 7,
            "provider_flag": True,
        },
        builder=builder,
    )

    first = factory.get_client("main")
    assert factory.get_client("main") is first
    assert factory.get_client("summary") is first
    assert len(built) == 1
    assert built[0].timeout_s == 7
    assert built[0].extra_body == {"provider_flag": True}


def test_llm_config_rejects_nonpositive_timeout() -> None:
    with pytest.raises(ValueError, match="timeout"):
        LLMConfig("https://example.test/v1", "key", "model", timeout_s=0)


def test_reasoning_effort_is_a_top_level_chat_parameter() -> None:
    client = OpenAICompatClient(
        LLMConfig(
            "https://example.test/v1",
            "key",
            "model",
            extra_body={"provider_flag": True},
        )
    )

    request = client._request(
        [ChatMessage(role="user", content="hi")],
        reasoning_effort="max",
    )

    assert request["reasoning_effort"] == "max"
    assert request["extra_body"] == {"provider_flag": True}
    assert "reasoning_effort" not in request["extra_body"]


@pytest.mark.parametrize("field", ["reasoning_content", "reasoning", "thinking"])
def test_plaintext_gateway_reasoning_fields_are_extracted(field: str) -> None:
    delta = type("Delta", (), {field: "可展示摘要"})()
    assert _reasoning_text(delta) == "可展示摘要"


def test_structured_or_opaque_reasoning_is_ignored() -> None:
    delta = type(
        "Delta",
        (),
        {
            "reasoning_content": {"encrypted": "opaque"},
            "model_extra": {"thinking": ["not", "plaintext"]},
        },
    )()
    assert _reasoning_text(delta) == ""


def test_chat_message_serializes_ephemeral_images_as_user_content_parts() -> None:
    attachment = ImageAttachment(
        path="diagram.png",
        data_url="data:image/png;base64,AA==",
        media_type="image/png",
        width=10,
        height=20,
    )
    message = ChatMessage(role="user", content="inspect", attachments=(attachment,))
    assert message.to_llm_dict()["content"] == [
        {"type": "text", "text": "inspect"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,AA==", "detail": "auto"},
        },
    ]
    assert "attachments" not in message.to_dict()


def test_openai_client_retries_explicit_vision_rejection_once_without_images() -> None:
    class VisionError(Exception):
        status_code = 400
        body = {"message": "image_url is unsupported by this model"}

    class Streaming:
        def __aiter__(self):
            async def iterate():
                choice = type(
                    "Choice",
                    (),
                    {
                        "delta": type("Delta", (), {"content": "ok", "tool_calls": ()})(),
                        "finish_reason": "stop",
                    },
                )()
                yield type("Chunk", (), {"choices": [choice]})()
            return iterate()

    class Completions:
        def __init__(self):
            self.calls = []

        async def create(self, **request):
            self.calls.append(request)
            if len(self.calls) == 1:
                raise VisionError("image content part is not supported")
            return Streaming()

    completions = Completions()
    sdk = type("SDK", (), {"chat": type("Chat", (), {"completions": completions})()})()
    client = OpenAICompatClient(
        LLMConfig("https://example.test/v1", "key", "model"), sdk_client=sdk
    )
    attachment = ImageAttachment(
        "image.png", "data:image/png;base64,AA==", "image/png", 1, 1
    )

    async def collect():
        return [
            chunk
            async for chunk in client.stream(
                [ChatMessage(role="user", content="look", attachments=(attachment,))]
            )
        ]

    chunks = __import__("asyncio").run(collect())
    assert chunks[0].content_delta == "ok"
    assert len(completions.calls) == 2
    assert isinstance(completions.calls[0]["messages"][0]["content"], list)
    assert "does not support visual input" in completions.calls[1]["messages"][0]["content"]


def test_factory_accepts_an_application_config_object() -> None:
    class AppProviderConfig:
        base_url = "https://example.test/v1"
        api_key = "secret"
        model = "model"
        max_tokens = 123
        timeout_s = 9
        temperature = 0.2

    captured = []
    factory = LLMClientFactory(
        AppProviderConfig(), builder=lambda config: captured.append(config) or FakeClient()
    )

    factory.get_client("main")
    assert captured[0].timeout_s == 9
    assert captured[0].model == "model"


async def test_openai_missing_raises_actionable_runtime_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = __import__

    def blocked_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "openai":
            raise ImportError("No module named 'openai'")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", blocked_import)

    client = OpenAICompatClient(LLMConfig("https://example.test/v1", "key", "model"))
    with pytest.raises(RuntimeError, match=r"pip install -r requirements\.txt"):
        await client.complete([ChatMessage(role="user", content="hi")])
