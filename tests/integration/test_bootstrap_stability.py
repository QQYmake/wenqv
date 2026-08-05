"""Integration tests for bootstrap identity stability and API guards.

Covers the behaviors that keep a visitor's identity stable across page
refreshes (cookie reuse on repeated GET /api/bootstrap), the cookie attribute
contract on the http test transport, and the 401 guard on private API paths in
the fully assembled application.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.api.middleware import WORKSPACE_COOKIE_NAME
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


def _cookie_value(set_cookie: str) -> str:
    """Extract the workspace_id value from a Set-Cookie header."""
    prefix = f"{WORKSPACE_COOKIE_NAME}="
    value = set_cookie.split(";", 1)[0]
    assert value.startswith(prefix), set_cookie
    return value[len(prefix) :]


def test_bootstrap_second_call_reuses_same_workspace_id(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        first = client.get("/api/bootstrap")
        assert first.status_code == 200
        workspace_id = first.json()["workspace_id"]

        # The cookie value issued by the server matches the response body.
        assert _cookie_value(first.headers["set-cookie"]) == workspace_id

        # A second bootstrap (e.g. a page refresh) must keep the same identity:
        # the cookie is the source of truth and must never be rotated.
        second = client.get("/api/bootstrap")
        assert second.status_code == 200
        assert second.json()["workspace_id"] == workspace_id
        assert _cookie_value(second.headers["set-cookie"]) == workspace_id


def test_bootstrap_identity_survives_refresh_across_sessions(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        boot = client.get("/api/bootstrap").json()
        created = client.post("/api/sessions", json={"title": "kept"}).json()

        # Simulate the frontend calling bootstrap again on mount after a reload.
        refresh = client.get("/api/bootstrap").json()
        assert refresh["workspace_id"] == boot["workspace_id"]

        # Previously created sessions are still visible after the refresh.
        listed = client.get("/api/sessions").json()["sessions"]
        assert [s["id"] for s in listed] == [created["id"]]


def test_bootstrap_reuses_valid_preseeded_cookie(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        client.cookies.set(WORKSPACE_COOKIE_NAME, "ws-stable")
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        assert response.json()["workspace_id"] == "ws-stable"


def test_bootstrap_replaces_invalid_cookie_with_fresh_identity(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        client.cookies.set(WORKSPACE_COOKIE_NAME, "bad value!")
        response = client.get("/api/bootstrap")
        assert response.status_code == 200
        workspace_id = response.json()["workspace_id"]
        # The malformed cookie is not reused; a fresh valid identity is issued.
        assert workspace_id != "bad value!"
        assert _cookie_value(response.headers["set-cookie"]) == workspace_id


def test_bootstrap_cookie_flags_on_http_transport(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        response = client.get("/api/bootstrap")
    set_cookie = response.headers["set-cookie"]
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    # cookie_secure=False is the http test transport: the Secure flag must be
    # dropped so the cookie can round-trip (Secure cookies are ignored by
    # browsers over http).
    assert "Secure" not in set_cookie


def test_private_api_requires_identity_without_cookie(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path), agent=FakeAgent())
    with TestClient(app) as client:
        # Every private API path is guarded, GET and POST alike, when the
        # client has no identity yet (checks must run before bootstrapping:
        # the bootstrap response itself issues the cookie).
        assert client.get("/api/sessions").status_code == 401
        assert client.get("/api/config").status_code == 401
        assert client.get("/api/skills").status_code == 401
        assert (
            client.post(
                "/api/chat",
                json={"session_id": "s1", "message": "hi"},
            ).status_code
            == 401
        )
        assert (
            client.post(
                "/api/chat/abort", json={"session_id": "s1"}
            ).status_code
            == 401
        )

        # Whitelisted endpoints are reachable without any identity.
        assert client.get("/api/bootstrap").status_code == 200
        assert client.get("/api/health").status_code == 200


def test_bootstrap_grants_identity_for_following_requests(tmp_path: Path) -> None:
    app = create_app(_config(tmp_path, cookie_secure=False), agent=FakeAgent())
    with TestClient(app) as client:
        assert client.get("/api/sessions").status_code == 401
        client.get("/api/bootstrap")
        # After bootstrapping, the cookie identity unlocks the private API.
        assert client.get("/api/sessions").status_code == 200
        assert client.get("/api/config").status_code == 200
