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


class ResolverRecordingAgent:
    """FakeAgent that records (workspace_id, selected client) per turn."""

    persists_messages = False

    def __init__(self, resolver):
        self.resolver = resolver
        self.captured = []

    async def stream(self, **kwargs):
        ws = kwargs.get("workspace_id")
        self.captured.append((ws, self.resolver.get_client("main", ws)))
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

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
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


def _default_llm() -> LLMSettings:
    return LLMSettings(
        main=LLMProviderConfig(
            base_url="https://default.example.com/v1",
            api_key="sk-default",
            model="default-model",
        )
    )


def test_get_config_never_returns_plaintext_for_configured_workspace(tmp_path: Path) -> None:
    store = _make_store()
    resolver, repo = _resolver(store, _default_llm())
    app = create_app(
        _config(tmp_path, default_llm=_default_llm()),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-get"}) as client:
        client.put("/api/user/config", json=_client_config())
        response = client.get("/api/user/config")
        assert response.status_code == 200
        body = response.json()
        assert body["has_config"] is True
        assert body["main"]["api_key"] == "sk-***345"
        assert body["summary"]["api_key"] == ""
        assert "sk-user-secret" not in response.text


def test_masked_roundtrip_keeps_stored_key(tmp_path: Path) -> None:
    default_llm = _default_llm()
    store = _make_store()
    resolver, repo = _resolver(store, default_llm)
    agent = ResolverCapturingAgent(resolver)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=agent,
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-roundtrip"}) as client:
        client.put("/api/user/config", json=_client_config())
        masked = client.get("/api/user/config").json()
        assert masked["main"]["api_key"] == "sk-***345"
        # Re-save with an empty api_key — exactly what the Settings UI sends
        # after a masked round-trip — the stored key must survive.
        roundtrip = {
            "main": {"base_url": masked["main"]["base_url"], "api_key": "", "model": masked["main"]["model"]},
            "summary": {"base_url": "", "api_key": "", "model": ""},
        }
        saved = client.put("/api/user/config", json=roundtrip).json()
        assert saved["main"]["api_key"] == "sk-***345"
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
        assert agent.captured is not None
        assert agent.captured.api_key == "sk-user-secret-12345"


def test_user_config_isolated_per_workspace(tmp_path: Path) -> None:
    default_llm = _default_llm()
    store = _make_store()
    resolver, repo = _resolver(store, default_llm)
    agent = ResolverRecordingAgent(resolver)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=agent,
    )
    with TestClient(app) as client:
        # Workspace A gets a user config.
        put = client.put("/api/user/config", json=_client_config(), headers={"X-Workspace-ID": "ws-a"})
        assert put.status_code == 200
        session_a = client.post("/api/sessions", json={}, headers={"X-Workspace-ID": "ws-a"}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session_a["id"], "message": "hi"},
            headers={"X-Workspace-ID": "ws-a"},
        ) as response:
            assert response.status_code == 200
        # Workspace B sees no user config and falls back to the default.
        empty = client.get("/api/user/config", headers={"X-Workspace-ID": "ws-b"}).json()
        assert empty["has_config"] is False
        assert empty["main"]["api_key"] == ""
        assert empty["summary"]["api_key"] == ""
        session_b = client.post("/api/sessions", json={}, headers={"X-Workspace-ID": "ws-b"}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session_b["id"], "message": "hi"},
            headers={"X-Workspace-ID": "ws-b"},
        ) as response:
            assert response.status_code == 200

    assert [(ws, client.api_key) for ws, client in agent.captured] == [
        ("ws-a", "sk-user-secret-12345"),
        ("ws-b", "sk-default"),
    ]


def test_put_invalidates_resolver_cache(tmp_path: Path) -> None:
    default_llm = _default_llm()
    store = _make_store()
    resolver, repo = _resolver(store, default_llm)
    agent = ResolverRecordingAgent(resolver)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=agent,
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-cache"}) as client:
        client.put("/api/user/config", json=_client_config())
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
        assert agent.captured[-1][1].api_key == "sk-user-secret-12345"
        # PUT a changed config; the very next chat must resolve the new values.
        updated = {
            "main": {"base_url": "https://api.example.com/v1", "api_key": "sk-new-key-987654", "model": "gpt-test"},
            "summary": {"base_url": "", "api_key": "", "model": ""},
        }
        client.put("/api/user/config", json=updated)
        with client.stream(
            "POST", "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
        assert agent.captured[-1][1].api_key == "sk-new-key-987654"
        assert agent.captured[-1][1].model == "gpt-test"


def test_test_endpoint_probes_each_role_independently(tmp_path: Path) -> None:
    default_llm = _default_llm()
    store = _make_store()

    def builder(provider):
        if provider.api_key == "sk-bad-summary":
            raise RuntimeError("summary provider rejected")
        return FakeClient(provider.base_url, provider.api_key, provider.model)

    resolver, repo = _resolver(store, default_llm, builder=builder)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    body = {
        "main": {"base_url": "https://api.example.com/v1", "api_key": "sk-user-secret-12345", "model": "gpt-test"},
        "summary": {"base_url": "https://summary.example.com/v1", "api_key": "sk-bad-summary", "model": "summary-model"},
    }
    with TestClient(app, headers={"X-Workspace-ID": "ws-probe"}) as client:
        response = client.post("/api/user/config/test", json=body)
        assert response.status_code == 200
        payload = response.json()
        # Main succeeds but the summary role fails: the endpoint must report it.
        assert payload["ok"] is False
        assert "summary" in payload["detail"]
        assert payload["roles"]["main"]["ok"] is True
        assert payload["roles"]["summary"]["ok"] is False


def test_test_endpoint_uses_stored_key_when_submitted_empty(tmp_path: Path) -> None:
    default_llm = _default_llm()
    store = _make_store()
    used = []

    def builder(provider):
        used.append(provider.api_key)
        return FakeClient(provider.base_url, provider.api_key, provider.model)

    resolver, repo = _resolver(store, default_llm, builder=builder)
    app = create_app(
        _config(tmp_path, default_llm=default_llm),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-stored"}) as client:
        client.put("/api/user/config", json=_client_config())
        response = client.post(
            "/api/user/config/test",
            json={
                "main": {"base_url": "", "api_key": "", "model": ""},
                "summary": {"base_url": "", "api_key": "", "model": ""},
            },
        )
        assert response.status_code == 200
        assert response.json()["ok"] is True
        # The stored (encrypted) key was decrypted and used for the probe.
        assert "sk-user-secret-12345" in used


def test_rotated_secret_degrades_gracefully_without_leak(tmp_path: Path) -> None:
    """A rotated AGENT_SECRET_KEY must never 500 or leak; stale rows read as empty.

    Contract reconciliation between the change-2 and change-3-5-6 forks:
    change-2 originally failed loud (clear 500 with an AGENT_SECRET_KEY hint);
    change-3-5-6 deliberately replaced that with per-row graceful degradation
    (``_decrypt_or_empty``) plus startup fail-fast for a malformed key (covered
    in test_config_guards.py). The merged contract is the graceful one: a
    restart with a different key keeps the app usable, shows the stored key as
    unset in Settings, and guides the user to re-enter it — while operator key
    misconfiguration is still caught at boot. The anti-leak guarantees of the
    original test (no plaintext key, no traceback) are preserved below.
    """

    import asyncio

    from cryptography.fernet import Fernet

    from server.storage.encryption import FernetCipher, KeyManager
    from server.storage.user_configs import UserConfigRepository

    db_path = tmp_path / "rotated.db"
    store = _make_store(db_path)
    cipher_a = FernetCipher(KeyManager(Fernet.generate_key().decode()))
    repo_a = UserConfigRepository(store, cipher_a)

    async def seed():
        await store.initialize()
        await repo_a.upsert(
            "ws-enc-fail",
            main={"base_url": "https://a.example.com", "api_key": "sk-user-secret-12345", "model": "m"},
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        await store.close()

    asyncio.run(seed())

    # The server is (re)started with a DIFFERENT AGENT_SECRET_KEY.
    cipher_b = FernetCipher(KeyManager(Fernet.generate_key().decode()))
    repo_b = UserConfigRepository(store, cipher_b)
    resolver_b, _ = _resolver(store, LLMSettings())
    app = create_app(
        _config(tmp_path),
        store=store, client_resolver=resolver_b, user_config_repo=repo_b, agent=FakeAgent(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "ws-enc-fail"}, raise_server_exceptions=False) as client:
        # The masked view degrades gracefully: 200, key read as unset, chat locked.
        get_response = client.get("/api/user/config")
        assert get_response.status_code == 200
        payload = get_response.json()
        assert payload["has_config"] is False
        assert payload["main"]["api_key"] == ""
        assert "sk-user-secret" not in get_response.text
        assert "Traceback" not in get_response.text

        # The connection-test endpoint probes the submitted body, never 500s.
        test_response = client.post("/api/user/config/test", json=_client_config())
        assert test_response.status_code == 200
        assert "sk-user-secret" not in test_response.text

        # Chat stays usable (no stored key -> 412 "API 未配置", not a 500).
        session = client.post("/api/sessions", json={}).json()
        chat_response = client.post(
            "/api/chat", json={"session_id": session["id"], "message": "hi"}
        )
        assert chat_response.status_code == 412
        assert "sk-user-secret" not in chat_response.text
        assert "Traceback" not in chat_response.text


def test_no_plaintext_at_rest_or_in_logs(tmp_path: Path, caplog) -> None:
    db_path = tmp_path / "leak.db"
    store = _make_store(db_path)
    resolver, repo = _resolver(store, _default_llm())
    app = create_app(
        _config(tmp_path, default_llm=_default_llm()),
        store=store, client_resolver=resolver, user_config_repo=repo, agent=FakeAgent(),
    )
    plaintext = "sk-user-secret-12345"
    with TestClient(app, headers={"X-Workspace-ID": "ws-leak"}) as client:
        client.put("/api/user/config", json=_client_config())
        client.get("/api/user/config")
        client.post("/api/user/config/test", json=_client_config())

    # Raw database files (and any WAL/SHM leftovers) must not contain the key.
    files = [path for path in tmp_path.iterdir() if path.is_file()]
    assert any(path.name == "leak.db" for path in files)
    for path in files:
        data = path.read_bytes()
        assert plaintext.encode() not in data, f"plaintext key found in {path.name}"
    # Logs emitted during the flows must not contain the key either.
    assert plaintext not in caplog.text
