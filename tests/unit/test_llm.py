from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from server.agent.llm import LLMClientFactory, LLMConfig, OpenAICompatClient
from server.agent.models import ChatMessage, LLMResponse, LLMStreamChunk


class FakeClient:
    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("ok")

    async def stream(self, messages, *, tools=None, max_tokens=None):
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
