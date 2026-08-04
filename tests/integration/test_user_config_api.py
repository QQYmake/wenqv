"""Integration tests for the per-user ClientResolver / user config endpoints."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.agent.models import LLMResponse
from server.config import (
    AppConfig,
    LLMProviderConfig,
    LLMSettings,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.main import create_app


class FakeAgent:
    persists_messages = False

    def __init__(self):
        self.workspace_ids = []

    async def stream(self, **kwargs):
        self.workspace_ids.append(kwargs.get("workspace_id"))
        yield {"type": "text_delta", "delta": "ok"}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, *_a, **_k):
        return False


class ResolverCapturingAgent:
    """FakeAgent that records which client the resolver selects for the turn."""

    persists_messages = False

    def __init__(self, resolver):
        self.resolver = resolver
        self.captured = None

    async def stream(self, **kwargs):
        ws = kwargs.get("workspace_id")
        self.captured = self.resolver.get_client("main", ws)
        yield {"type": "text_delta", "delta": "ok"}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, *_a, **_k):
        return False


class FakeClient:
    def __init__(self, base_url, api_key, model):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.completes = 0

    async def complete(self, messages, *, tools=None, max_tokens=None):
        self.completes += 1
        return LLMResponse(content="pong")

    async def stream(self, messages, *, tools=None, max_tokens=None):
        yield  # pragma: no cover


def _config(tmp_path: Path, *, default_llm: LLMSettings | None = None) -> AppConfig:
    return AppConfig(
        llm=default_llm or LLMSettings(),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=Path("missing-dist"),
            cors_origins=("http://localhost:5173",),
            cookie_secure=False,
        ),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path / "ws"),
    )


def _resolver(store, default_llm, builder=None):
    from server.llm_resolver import LLMResolverAdapter
    from server.storage.encryption import FernetCipher, KeyManager
    from server.storage.user_configs import UserConfigRepository
    from cryptography.fernet import Fernet

    repo = UserConfigRepository(store, FernetCipher(KeyManager(Fernet.generate_key().decode())))
    def default_builder(provider):
        return FakeClient(provider.base_url, provider.api_key, provider.model)
    return LLMResolverAdapter(repo, default_llm, builder=builder or default_builder), repo


def _client_config():
    return {
        "main": {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-user-secret-12345",
            "model": "gpt-test",
        },
        "summary": {
            "base_url": "",
            "api_key": "",
            "model": "",
        },
    }


def test_chat_returns_4xx_when_no_api_config_and_no_default(tmp_path: Path) -> None:
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(_config(tmp_path), store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent())
    with TestClient(app, headers={"X-Workspace-ID": "ws-x"}) as client:
        session = client.post("/api/sessions", json={}).json()
        response = client.post(
            "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        )
        assert response.status_code == 412
        assert response.json()["detail"] == "API 未配置"


def test_chat_uses_user_config_over_default(tmp_path: Path) -> None:
    default_llm = LLMSettings(
        main=LLMProviderConfig(
            base_url="https://default.example.com/v1",
            api_key="sk-default",
            model="default-model",
        )
    )
    store = _make_store()
    resolver, repo = _resolver(store, default_llm)
    built = []
    def builder(provider):
        c = FakeClient(provider.base_url, provider.api_key, provider.model)
        built.append(c)
        return c
    resolver2, repo2 = _resolver(store, default_llm, builder=builder)
    agent = ResolverCapturingAgent(resolver2)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store,
        client_resolver=resolver2,
        user_config_repo=repo2,
        agent=agent,
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-user"}) as client:
        # PUT a user config that overrides the default model.
        put = client.put("/api/user/config", json=_client_config())
        assert put.status_code == 200
        body = put.json()
        assert body["has_config"] is True
        assert body["main"]["model"] == "gpt-test"
        # Key is masked in the response.
        assert body["main"]["api_key"] == "sk-***345"
        assert "sk-user-secret" not in put.text

        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
        # The resolver selected the user's model (gpt-test), not the default.
        assert agent.captured is not None
        assert agent.captured.model == "gpt-test"
        assert agent.captured.api_key == "sk-user-secret-12345"


def test_put_user_config_stores_encrypted_key_only(tmp_path: Path) -> None:
    db_path = tmp_path / "enc.db"
    store = _make_store(db_path)
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path), store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent()
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-enc"}) as client:
        client.put("/api/user/config", json=_client_config())

    # The DB row holds ciphertext, never plaintext. Re-open the file DB to read.
    import asyncio
    async def check():
        fresh = _make_store(db_path)
        await fresh.initialize()
        row = await fresh.get_user_config("ws-enc")
        assert row["main_api_key_encrypted"] != "sk-user-secret-12345"
        assert "sk-user-secret" not in row["main_api_key_encrypted"]
        await fresh.close()
    asyncio.run(check())


def test_get_user_config_returns_masked_for_unconfigured(tmp_path: Path) -> None:
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path), store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent()
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-empty"}) as client:
        body = client.get("/api/user/config").json()
        assert body["has_config"] is False
        assert body["main"]["api_key"] == ""


def test_user_config_test_endpoint_succeeds_with_fake_client(tmp_path: Path) -> None:
    default_llm = LLMSettings(
        main=LLMProviderConfig(
            base_url="https://default.example.com/v1",
            api_key="sk-default",
            model="default-model",
        )
    )
    store = _make_store()
    resolver, repo = _resolver(store, default_llm)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-test"}) as client:
        response = client.post("/api/user/config/test", json=_client_config())
        assert response.status_code == 200
        assert response.json()["ok"] is True


def test_saved_config_survives_bootstrap_reload(tmp_path: Path) -> None:
    """Page reload (bootstrap again) must keep the same workspace identity.

    This mirrors the reported failure: main config saved successfully, but the
    chat flow afterwards saw "API 未配置" because every reload minted a brand
    new workspace id and the saved row belonged to the previous one.
    """
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app) as client:  # cookie jar shared across "reloads"
        boot1 = client.get("/api/bootstrap").json()
        put = client.put("/api/user/config", json=_client_config())
        assert put.status_code == 200

        # Simulate a reload: bootstrap runs again and must reuse the identity.
        boot2 = client.get("/api/bootstrap").json()
        assert boot2["workspace_id"] == boot1["workspace_id"]

        # The saved config is visible and chat works (no explicit header).
        assert client.get("/api/user/config").json()["has_config"] is True
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200


def test_user_config_test_endpoint_probes_summary_without_main(tmp_path: Path) -> None:
    """A summary-only config can be tested; the endpoint must not 422 on main."""
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-sum"}) as client:
        body = {
            "main": {"base_url": "", "api_key": "", "model": ""},
            "summary": {
                "base_url": "https://summary.example.com/v1",
                "api_key": "sk-summary-key-1",
                "model": "summary-model",
            },
        }
        response = client.post("/api/user/config/test", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert payload["roles"]["summary"]["ok"] is True
        assert "main" not in payload["roles"]
        assert "主模型未配置" in payload["detail"]


def test_user_config_test_endpoint_probes_both_roles(tmp_path: Path) -> None:
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-both"}) as client:
        body = {
            "main": {
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-user-secret-12345",
                "model": "gpt-test",
            },
            "summary": {
                "base_url": "https://summary.example.com/v1",
                "api_key": "sk-summary-key-1",
                "model": "summary-model",
            },
        }
        response = client.post("/api/user/config/test", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["ok"] is True
        assert set(payload["roles"]) == {"main", "summary"}
        assert all(probe["ok"] for probe in payload["roles"].values())


def test_user_config_test_endpoint_422_when_no_complete_role(tmp_path: Path) -> None:
    store = _make_store()
    resolver, repo = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-none"}) as client:
        body = {
            "main": {"base_url": "", "api_key": "", "model": ""},
            "summary": {"base_url": "", "api_key": "", "model": ""},
        }
        response = client.post("/api/user/config/test", json=body)
        assert response.status_code == 422
        assert "至少填写一个" in response.json()["detail"]


def _make_store(path=None):
    from server.storage import SQLiteStore
    if path is None:
        return SQLiteStore(":memory:")
    return SQLiteStore(path)