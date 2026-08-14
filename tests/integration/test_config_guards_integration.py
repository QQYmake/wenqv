"""Integration: the app starts with empty llm + require_user_config."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.config import (
    AppConfig,
    LLMSettings,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.config import load_config
from server.main import create_app


class FakeAgent:
    persists_messages = False

    async def stream(self, **kwargs):
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, *_a, **_k):
        return False


def _config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        llm=LLMSettings(require_user_config=True),  # empty main + require_user_config
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=Path("missing-dist"),
            cors_origins=("http://localhost:5173",),
            cookie_secure=False,
        ),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path / "ws"),
    )


def test_packaged_default_skills_survive_external_workspace_root(tmp_path: Path) -> None:
    project_root = Path(__file__).resolve().parents[2]
    config = load_config(
        project_root / "config.yaml",
        environ={
            "AGENT_REQUIRE_USER_CONFIG": "false",
            "AGENT_WORKSPACE_ROOT": str(tmp_path / "runtime-workspace"),
        },
    )
    config.storage.sqlite_path = Path(":memory:")
    config.server.static_dir = tmp_path / "missing-dist"

    app = create_app(config)
    with TestClient(app, headers={"X-Workspace-ID": "external-root-test"}) as client:
        response = client.get("/api/skills")

    assert response.status_code == 200
    assert "wenqu" in {item["name"] for item in response.json()["skills"]}


def test_app_starts_with_empty_llm_and_require_user_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Provide a secret so the cipher fail-fast does not trigger; the app should
    # still start even though llm.main is empty (users fill their own config).
    from cryptography.fernet import Fernet

    monkeypatch.setenv("AGENT_SECRET_KEY", Fernet.generate_key().decode())

    app = create_app(_config(tmp_path), agent=FakeAgent())
    with TestClient(app, headers={"X-Workspace-ID": "ws"}) as client:
        # The app is alive and the public config endpoint works.
        assert client.get("/api/health").status_code == 200
        # A chat attempt without any configured API returns the structured 412.
        session = client.post("/api/sessions", json={}).json()
        response = client.post(
            "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        )
        assert response.status_code == 412
        assert response.json()["detail"] == "API 未配置"
