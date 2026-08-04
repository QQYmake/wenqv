"""Unit tests for the LLMResolverAdapter (ClientResolver port)."""

from __future__ import annotations

import asyncio

from server.agent.ports import LLMClient
from server.config import LLMProviderConfig, LLMSettings
from server.llm_resolver import LLMResolverAdapter, NotConfiguredError
from server.storage.encryption import FernetCipher, KeyManager
from server.storage.sqlite import SQLiteStore
from server.storage.user_configs import UserConfigRepository


class RecordingClient(LLMClient):
    def __init__(self, base_url, api_key, model):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.completions = 0

    async def complete(self, messages, *, tools=None, max_tokens=None):
        self.completions += 1
        return type("R", (), {"content": "pong"})()

    async def stream(self, messages, *, tools=None, max_tokens=None):
        yield  # pragma: no cover


def _key():
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _make(store, *, default_llm, builder=None):
    repo = UserConfigRepository(store, FernetCipher(KeyManager(_key())))
    def default_builder(provider):
        return RecordingClient(provider.base_url, provider.api_key, provider.model)
    return LLMResolverAdapter(repo, default_llm, builder=builder or default_builder)


def test_client_resolver_returns_user_client_when_configured() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        default = LLMSettings(
            main=LLMProviderConfig(
                base_url="https://default.example.com/v1",
                api_key="sk-default",
                model="default-model",
            )
        )
        resolver = _make(store, default_llm=default)
        repo = resolver._repo  # noqa: SLF001
        await repo.upsert(
            "ws-user",
            main={
                "base_url": "https://user.example.com/v1",
                "api_key": "sk-user",
                "model": "user-model",
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await resolver.warm("ws-user")
        client = resolver.get_client("main", "ws-user")
        assert client.base_url == "https://user.example.com/v1"
        assert client.model == "user-model"
        assert client.api_key == "sk-user"
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_falls_back_to_default_when_field_missing() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        default = LLMSettings(
            main=LLMProviderConfig(
                base_url="https://default.example.com/v1",
                api_key="sk-default",
                model="default-model",
            )
        )
        resolver = _make(store, default_llm=default)
        repo = resolver._repo  # noqa: SLF001
        await repo.upsert(
            "ws-mixed",
            main={
                "base_url": "https://user.example.com/v1",
                "api_key": "sk-user",
                "model": "",  # falls back to default-model
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await resolver.warm("ws-mixed")
        client = resolver.get_client("main", "ws-mixed")
        assert client.base_url == "https://user.example.com/v1"
        assert client.model == "default-model"
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_returns_no_config_when_neither_user_nor_default() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        # Empty default config.
        resolver = _make(store, default_llm=LLMSettings())
        await resolver.warm("ws-empty")
        assert resolver.has_config("ws-empty") is False
        with __import__("pytest").raises(NotConfiguredError):
            resolver.get_client("main", "ws-empty")
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_caches_client_per_base_url_api_key_model() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        resolver = _make(store, default_llm=LLMSettings())
        repo = resolver._repo  # noqa: SLF001
        await repo.upsert(
            "ws-a",
            main={"base_url": "https://a", "api_key": "k1", "model": "m"},
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await repo.upsert(
            "ws-b",
            main={"base_url": "https://a", "api_key": "k1", "model": "m"},
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await resolver.warm("ws-a")
        await resolver.warm("ws-b")
        c1 = resolver.get_client("main", "ws-a")
        c2 = resolver.get_client("main", "ws-b")
        # Same (base_url, api_key, model) -> same pooled client instance.
        assert c1 is c2
        assert len(resolver._pool) == 1  # noqa: SLF001
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_has_config_uses_default_without_warm() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        default = LLMSettings(
            main=LLMProviderConfig(
                base_url="https://default.example.com/v1",
                api_key="sk-default",
                model="default-model",
            )
        )
        resolver = _make(store, default_llm=default)
        # No warm() call: has_config answers from the default config.
        assert resolver.has_config("never-warmed") is True
        await store.close()

    asyncio.run(scenario())