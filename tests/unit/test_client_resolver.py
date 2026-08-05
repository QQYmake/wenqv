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


def test_client_resolver_summary_falls_back_to_user_main_when_unconfigured() -> None:
    """An unconfigured summary role reuses the *user's* main provider."""

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
            "ws-main-only",
            main={
                "base_url": "https://user.example.com/v1",
                "api_key": "sk-user",
                "model": "user-model",
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await resolver.warm("ws-main-only")
        main_client = resolver.get_client("main", "ws-main-only")
        summary_client = resolver.get_client("summary", "ws-main-only")
        # Both roles resolve to the user's main provider, not the app default.
        assert summary_client.base_url == main_client.base_url == "https://user.example.com/v1"
        assert summary_client.api_key == "sk-user"
        assert summary_client.model == "user-model"
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_roles_are_independent_when_both_configured() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        resolver = _make(store, default_llm=LLMSettings())
        repo = resolver._repo  # noqa: SLF001
        await repo.upsert(
            "ws-both",
            main={
                "base_url": "https://main.example.com/v1",
                "api_key": "sk-main",
                "model": "main-model",
            },
            summary={
                "base_url": "https://summary.example.com/v1",
                "api_key": "sk-summary",
                "model": "summary-model",
            },
        )
        await resolver.warm("ws-both")
        main_client = resolver.get_client("main", "ws-both")
        summary_client = resolver.get_client("summary", "ws-both")
        assert main_client.base_url == "https://main.example.com/v1"
        assert main_client.api_key == "sk-main"
        assert main_client.model == "main-model"
        assert summary_client.base_url == "https://summary.example.com/v1"
        assert summary_client.api_key == "sk-summary"
        assert summary_client.model == "summary-model"
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_pool_evicts_least_recently_used() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, FernetCipher(KeyManager(_key())))

        def builder(provider):
            return RecordingClient(provider.base_url, provider.api_key, provider.model)

        resolver = LLMResolverAdapter(
            repo, LLMSettings(), builder=builder, pool_size=2, cache_size=8
        )
        configs = [
            ("ws-1", "https://a.example.com", "sk-a", "m-a"),
            ("ws-2", "https://b.example.com", "sk-b", "m-b"),
            ("ws-3", "https://c.example.com", "sk-c", "m-c"),
        ]
        for ws, url, key, model in configs:
            await repo.upsert(
                ws,
                main={"base_url": url, "api_key": key, "model": model},
                summary={"base_url": "", "api_key": "", "model": ""},
            )
            await resolver.warm(ws)
        first = resolver.get_client("main", "ws-1")
        resolver.get_client("main", "ws-2")
        resolver.get_client("main", "ws-3")
        # The least-recently-used client (ws-1) was evicted from the pool.
        assert len(resolver._pool) == 2  # noqa: SLF001
        assert "https://a.example.com" not in {c.base_url for c in resolver._pool.values()}
        # Re-resolving the evicted key builds a fresh client.
        rebuilt = resolver.get_client("main", "ws-1")
        assert rebuilt is not first
        assert rebuilt.base_url == "https://a.example.com"
        assert len(resolver._pool) == 2  # noqa: SLF001
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_config_cache_evicts_lru_and_falls_back_to_default() -> None:
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
        repo = UserConfigRepository(store, FernetCipher(KeyManager(_key())))

        def builder(provider):
            return RecordingClient(provider.base_url, provider.api_key, provider.model)

        resolver = LLMResolverAdapter(
            repo, default, builder=builder, pool_size=8, cache_size=2
        )
        for ws in ("ws-a", "ws-b", "ws-c"):
            await repo.upsert(
                ws,
                main={"base_url": f"https://{ws}.example.com", "api_key": f"sk-{ws}", "model": "m"},
                summary={"base_url": "", "api_key": "", "model": ""},
            )
            await resolver.warm(ws)
        # The config cache kept only the two most recently warmed workspaces.
        assert list(resolver._config_cache) == ["ws-b", "ws-c"]
        # The evicted workspace falls back to the default config in the sync path.
        client = resolver.get_client("main", "ws-a")
        assert client.base_url == "https://default.example.com/v1"
        assert resolver.has_config("ws-a") is True
        await store.close()

    asyncio.run(scenario())


def test_client_resolver_rejects_unknown_role() -> None:
    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        resolver = _make(store, default_llm=LLMSettings())
        with __import__("pytest").raises(ValueError):
            resolver.get_client("embedding", "ws-any")
        await store.close()

    asyncio.run(scenario())