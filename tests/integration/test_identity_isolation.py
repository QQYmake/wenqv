"""Integration tests for the bootstrap cookie and workspace file isolation."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.config import (
    AppConfig,
    LLMProviderConfig,
    LLMSettings,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.main import create_app
from server.storage import IsolatedWorkspaceResolver


class FakeAgent:
    persists_messages = False

    async def stream(self, **kwargs):
        yield {"type": "text_delta", "delta": "ok"}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, *_a, **_k):
        return False


def _config(tmp_path: Path, *, cookie_secure: bool = False) -> AppConfig:
    return AppConfig(
        llm=LLMSettings(
            main=LLMProviderConfig(
                base_url="https://example.invalid/v1", api_key="secret", model="fake"
            )
        ),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=Path("missing-dist"),
            cors_origins=("http://localhost:5173",),
            cookie_secure=cookie_secure,
        ),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path / "workspaces"),
    )


def test_bootstrap_sets_httponly_secure_cookie(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=True), agent=FakeAgent())
    with TestClient(app) as client:
        response = client.get("/api/bootstrap")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace_id"]

    set_cookie = response.headers["set-cookie"]
    # Cookie carries the workspace id and the required security flags.
    assert "workspace_id=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie


def test_bootstrap_then_chat_flow_end_to_end(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        # 1. New visitor bootstraps; the TestClient jar stores the cookie and
        #    sends it back on subsequent requests (cookie_secure=False so the
        #    http test transport keeps the cookie).
        boot = client.get("/api/bootstrap")
        assert boot.status_code == 200
        workspace_id = boot.json()["workspace_id"]

        # 2. The cookie is the only identity carried; AuthMiddleware injects it
        #    as X-Workspace-ID for downstream dependencies. Creating a session
        #    works without any explicit header.
        session = client.post("/api/sessions", json={"title": "First"}).json()
        assert session["workspace_id"] == workspace_id

        # 3. Chat streams to completion with the cookie identity.
        with client.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
            lines = [
                line.removeprefix("data: ")
                for line in response.iter_lines()
                if line.startswith("data: ")
            ]
        import json

        events = [json.loads(line) for line in lines]
        assert events[-1]["type"] == "done"

        # 4. The session appears under this workspace only.
        listed = client.get("/api/sessions").json()["sessions"]
        assert [s["id"] for s in listed] == [session["id"]]


def test_two_workspaces_cannot_see_each_other_sessions(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path), agent=FakeAgent())
    with TestClient(app) as client_a, TestClient(app) as client_b:
        a = client_a.get("/api/bootstrap").json()
        b = client_b.get("/api/bootstrap").json()
        assert a["workspace_id"] != b["workspace_id"]

        session_a = client_a.post("/api/sessions", json={"title": "A"}).json()
        client_b.post("/api/sessions", json={"title": "B"})

        # Each client only sees its own sessions via its cookie.
        assert [s["title"] for s in client_a.get("/api/sessions").json()["sessions"]] == ["A"]
        assert [s["title"] for s in client_b.get("/api/sessions").json()["sessions"]] == ["B"]

        # B cannot read A's messages (workspace-scoped store query).
        assert (
            client_b.get(f"/api/sessions/{session_a['id']}/messages").status_code == 404
        )
        # B cannot rename or delete A's session.
        assert client_b.patch(
            f"/api/sessions/{session_a['id']}", json={"title": "hacked"}
        ).status_code == 404
        assert client_b.delete(f"/api/sessions/{session_a['id']}").status_code == 404

        # File roots are isolated: each workspace gets its own directory.
        resolver = IsolatedWorkspaceResolver(tmp_path / "workspaces")
        root_a = resolver(a["workspace_id"])
        root_b = resolver(b["workspace_id"])
        assert root_a != root_b
        (root_a / "private.txt").write_text("only-A", encoding="utf-8")
        assert not (root_b / "private.txt").exists()