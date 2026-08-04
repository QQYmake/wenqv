"""Unit tests for the AuthMiddleware (cookie/header identity + whitelist)."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from server.api.middleware import AuthMiddleware, WORKSPACE_COOKIE_NAME


def _build_app() -> FastAPI:
    app = FastAPI()

    @app.get("/api/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/bootstrap")
    async def bootstrap():
        return {"workspace_id": "issued"}

    @app.post("/api/user/config/test")
    async def config_test():
        return {"ok": True}

    @app.get("/api/sessions")
    async def list_sessions(request: Request):
        return {"workspace_id": request.headers.get("X-Workspace-ID")}

    @app.get("/")
    async def index():
        return {"page": "spa"}

    app.add_middleware(AuthMiddleware)
    return app


def test_auth_middleware_injects_workspace_id_from_cookie() -> None:
    app = _build_app()
    with TestClient(app) as client:
        client.cookies.set(WORKSPACE_COOKIE_NAME, "ws-from-cookie")
        # Cookie value is injected as X-Workspace-ID for downstream handlers.
        response = client.get("/api/sessions")
    assert response.status_code == 200
    assert response.json()["workspace_id"] == "ws-from-cookie"


def test_auth_middleware_accepts_header_when_no_cookie() -> None:
    app = _build_app()
    with TestClient(app) as client:
        response = client.get(
            "/api/sessions", headers={"X-Workspace-ID": "ws-from-header"}
        )
    assert response.status_code == 200
    assert response.json()["workspace_id"] == "ws-from-header"


def test_auth_middleware_cookie_takes_precedence_over_header() -> None:
    app = _build_app()
    with TestClient(app) as client:
        client.cookies.set(WORKSPACE_COOKIE_NAME, "ws-cookie")
        response = client.get(
            "/api/sessions",
            headers={"X-Workspace-ID": "ws-header"},
        )
    assert response.status_code == 200
    assert response.json()["workspace_id"] == "ws-cookie"


def test_auth_middleware_rejects_api_without_cookie_but_allows_whitelist() -> None:
    app = _build_app()
    with TestClient(app) as client:
        # Private API path, no identity -> 401.
        assert client.get("/api/sessions").status_code == 401

        # Whitelisted API paths are reachable without identity.
        assert client.get("/api/health").status_code == 200
        assert client.get("/api/bootstrap").status_code == 200
        assert client.post("/api/user/config/test").status_code == 200

        # Non-API (static/SPA) paths are always reachable.
        assert client.get("/").status_code == 200

        # Invalid cookie value is rejected.
        client.cookies.set(WORKSPACE_COOKIE_NAME, "bad value!")
        bad = client.get("/api/sessions")
        assert bad.status_code == 401


def test_auth_middleware_rejects_invalid_workspace_id_shape() -> None:
    app = _build_app()
    with TestClient(app) as client:
        for bad in ("", " ", "1 Leading-digits-ok-but-!bad", "x" * 200):
            response = client.get("/api/sessions", headers={"X-Workspace-ID": bad})
            assert response.status_code == 401, bad