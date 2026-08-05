"""End-to-end gateway test: two users, fully isolated, per the task spec."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from server.agent.models import LLMResponse
from server.config import (
    AppConfig,
    LLMSettings,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.main import create_app
from server.storage.encryption import FernetCipher, KeyManager
from server.storage.user_configs import UserConfigRepository
from server.llm_resolver import LLMResolverAdapter


class FakeAgent:
    """Minimal agent: stores the user message and finishes."""

    persists_messages = False

    async def stream(self, **kwargs):
        yield {"type": "text_delta", "delta": "ok"}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, *_a, **_k):
        return False


class FakeClient:
    def __init__(self, base_url, api_key, model):
        self.base_url = base_url
        self.api_key = api_key
        self.model = model

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse(content="pong")

    async def stream(self, messages, *, tools=None, max_tokens=None):
        yield  # pragma: no cover


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        llm=LLMSettings(),  # no default; users configure their own
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=Path("missing-dist"),
            cors_origins=("http://localhost:5173",),
            cookie_secure=False,  # so the http TestClient round-trips the cookie
        ),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path / "ws"),
    )


def _wiring(store):
    from cryptography.fernet import Fernet

    cipher = FernetCipher(KeyManager(Fernet.generate_key().decode()))
    repo = UserConfigRepository(store, cipher)
    resolver = LLMResolverAdapter(
        repo, LLMSettings(), builder=lambda p: FakeClient(p.base_url, p.api_key, p.model)
    )
    return cipher, repo, resolver


def _user_config():
    return {
        "main": {
            "base_url": "https://api.example.com/v1",
            "api_key": "sk-user-secret-12345",
            "model": "gpt-test",
        },
        "summary": {"base_url": "", "api_key": "", "model": ""},
    }


def test_multi_user_flow_end_to_end(tmp_path: Path) -> None:
    from server.storage import SQLiteStore

    store = SQLiteStore(":memory:")
    cipher, repo, resolver = _wiring(store)
    app = create_app(
        _config(tmp_path),
        store=store,
        cipher=cipher,
        user_config_repo=repo,
        client_resolver=resolver,
        agent=FakeAgent(),
    )

    with TestClient(app) as client_a, TestClient(app) as client_b:
        # 1. Each visitor bootstraps a distinct workspace identity cookie.
        a = client_a.get("/api/bootstrap").json()
        b = client_b.get("/api/bootstrap").json()
        assert a["workspace_id"] != b["workspace_id"]

        # 2. Each configures their own API key.
        put_a = client_a.put("/api/user/config", json=_user_config())
        put_b = client_b.put("/api/user/config", json=_user_config())
        assert put_a.status_code == 200 and put_b.status_code == 200
        # The API returns masked keys, never plaintext.
        assert put_a.json()["main"]["api_key"] == "sk-***345"
        assert "sk-user-secret" not in put_a.text

        # 3. Each chats; sessions are mutually invisible.
        session_a = client_a.post("/api/sessions", json={"title": "A"}).json()
        session_b = client_b.post("/api/sessions", json={"title": "B"}).json()
        with client_a.stream(
            "POST", "/api/chat",
            json={"session_id": session_a["id"], "message": "hi"},
        ) as resp_a:
            assert resp_a.status_code == 200
        with client_b.stream(
            "POST", "/api/chat",
            json={"session_id": session_b["id"], "message": "hi"},
        ) as resp_b:
            assert resp_b.status_code == 200

        assert [s["title"] for s in client_a.get("/api/sessions").json()["sessions"]] == ["A"]
        assert [s["title"] for s in client_b.get("/api/sessions").json()["sessions"]] == ["B"]
        # B cannot read A's messages.
        assert (
            client_b.get(f"/api/sessions/{session_a['id']}/messages").status_code == 404
        )

        # File roots are isolated: each workspace has a private directory.
        from server.storage import IsolatedWorkspaceResolver

        root = IsolatedWorkspaceResolver(tmp_path / "ws")
        root_a = root(a["workspace_id"])
        root_b = root(b["workspace_id"])
        assert root_a != root_b
        (root_a / "a.txt").write_text("only-A", encoding="utf-8")
        assert not (root_b / "a.txt").exists()

        # 4. The connection-test endpoint succeeds for each user.
        test_a = client_a.post("/api/user/config/test", json=_user_config())
        test_b = client_b.post("/api/user/config/test", json=_user_config())
        assert test_a.status_code == 200 and test_a.json()["ok"] is True
        assert test_b.status_code == 200 and test_b.json()["ok"] is True

        # 5. The DB stores ciphertext; the stored key is not the plaintext.
        async def check_rows():
            await store.initialize()
            row_a = await store.get_user_config(a["workspace_id"])
            row_b = await store.get_user_config(b["workspace_id"])
            assert row_a["main_api_key_encrypted"] != "sk-user-secret-12345"
            assert "sk-user-secret" not in row_a["main_api_key_encrypted"]
            # Each workspace has its own row keyed by workspace_id.
            assert row_a["workspace_id"] == a["workspace_id"]
            assert row_b["workspace_id"] == b["workspace_id"]

        asyncio.run(check_rows())