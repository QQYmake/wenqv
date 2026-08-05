"""Integration tests for CORS credentials and the SPA whitelist interaction."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import (
    AppConfig,
    LLMSettings,
    LLMProviderConfig,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.main import create_app


def _config(*, cors: tuple[str, ...], static_dir: Path | None = None) -> AppConfig:
    return AppConfig(
        llm=LLMSettings(
            main=LLMProviderConfig(
                base_url="https://example.invalid/v1", api_key="secret", model="fake"
            )
        ),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=static_dir or Path("missing-dist"),
            cors_origins=cors,
        ),
        workspace=WorkspaceSettings(default_id="default", root=Path.cwd()),
    )


def test_cors_wildcard_origin_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        create_app(_config(cors=("*",)))


def test_cors_allows_credentials_with_explicit_origin() -> None:
    app = create_app(_config(cors=("https://app.example.com",)))
    with TestClient(app) as client:
        response = client.options(
            "/api/health",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "https://app.example.com"
    assert response.headers["access-control-allow-credentials"] == "true"
    allow_headers = response.headers["access-control-allow-headers"].lower()
    assert "content-type" in allow_headers
    assert "x-workspace-id" in allow_headers


def test_cors_rejects_when_origin_not_listed() -> None:
    app = create_app(_config(cors=("https://app.example.com",)))
    with TestClient(app) as client:
        # Preflight from an unlisted origin is rejected by Starlette's CORS
        # middleware with 400 "Disallowed CORS origin".
        preflight = client.options(
            "/api/health",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert preflight.status_code == 400

        # A simple (non-preflight) request from an unlisted origin is processed
        # but receives no CORS allow-origin header, so the browser cannot read it.
        simple = client.get(
            "/api/health", headers={"Origin": "https://evil.example.com"}
        )
        assert simple.status_code == 200
        assert "access-control-allow-origin" not in simple.headers


def test_new_visitor_can_load_index_html_without_cookie(tmp_path: Path) -> None:
    static = tmp_path / "dist"
    static.mkdir()
    (static / "index.html").write_text("<!doctype html><title>SPA</title>", encoding="utf-8")
    (static / "assets").mkdir()
    (static / "assets" / "app.js").write_text("// bundle", encoding="utf-8")

    app = create_app(_config(cors=("http://localhost:5173",), static_dir=static))
    with TestClient(app) as client:
        # Brand-new visitor: no cookie, no header. The SPA shell must load.
        index = client.get("/")
        assert index.status_code == 200
        assert "SPA" in index.text

        # A client-side route falls back to index.html (no dot in the name).
        deep = client.get("/some/workspace/route")
        assert deep.status_code == 200
        assert "SPA" in deep.text

        # Static assets are reachable too.
        asset = client.get("/assets/app.js")
        assert asset.status_code == 200
        assert "bundle" in asset.text

        # Private API still requires identity even when static is mounted.
        assert client.get("/api/sessions").status_code == 401


def test_cors_preflight_on_private_api_path_needs_no_identity() -> None:
    """CORS answers preflights before the Auth guard runs.

    A preflight targets the private ``/api/sessions`` path and carries no
    workspace identity; it must still be answered by the outermost CORS
    middleware with the full preflight contract, not a 401.
    """
    app = create_app(_config(cors=("https://app.example.com",)))
    with TestClient(app) as client:
        preflight = client.options(
            "/api/sessions",
            headers={
                "Origin": "https://app.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "https://app.example.com"
    assert preflight.headers["access-control-allow-credentials"] == "true"
    assert "GET" in preflight.headers["access-control-allow-methods"]


def test_cors_headers_are_added_to_auth_rejection() -> None:
    """Auth-rejected responses stay browser-readable because CORS is outermost.

    If CORS were inside the Auth guard, a 401 raised before it would reach the
    browser without ``access-control-allow-origin``, and the SPA could not
    distinguish an identity error from a network failure.
    """
    app = create_app(_config(cors=("https://app.example.com",)))
    with TestClient(app) as client:
        denied = client.get(
            "/api/sessions", headers={"Origin": "https://app.example.com"}
        )
    assert denied.status_code == 401
    assert denied.headers["access-control-allow-origin"] == "https://app.example.com"
    assert denied.headers["access-control-allow-credentials"] == "true"
    assert denied.json()["detail"] == "Missing workspace identity"