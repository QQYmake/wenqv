from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from server.config import AppConfig, LLMProviderConfig, LLMSettings, ServerSettings, StorageSettings, WorkspaceSettings
from server.main import create_app
from server.services.document_exporter import DocumentExporter
from server.storage import SQLiteStore


class IdleAgent:
    persists_messages = False

    async def stream(self, **kwargs):
        yield {"type": "done", "finish_reason": "stop"}


def test_download_api_returns_attachment_without_exposing_path(tmp_path: Path) -> None:
    config = AppConfig(
        llm=LLMSettings(
            main=LLMProviderConfig(
                base_url="https://example.invalid/v1",
                api_key="secret",
                model="fake-main",
            )
        ),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(static_dir=Path("missing-dist"), cookie_secure=False),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path),
    )
    exporter = DocumentExporter()
    workspace_root = tmp_path / "default"
    exported = exporter.export(
        filename="中文报告",
        format="txt",
        content="# 标题\n\n正文",
        workspace_root=workspace_root,
    )
    store = SQLiteStore(":memory:")
    app = create_app(
        config,
        store=store,
        agent=IdleAgent(),
        document_exporter=exporter,
    )

    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        response = client.get(exported.download_url)
        assert response.status_code == 200
        assert response.content == b"\xe6\xa0\x87\xe9\xa2\x98\n\n\xe6\xad\xa3\xe6\x96\x87"
        assert response.headers["content-type"].startswith("text/plain")
        assert "attachment" in response.headers["content-disposition"]
        assert "中文报告.txt" in response.headers["content-disposition"] or "filename*=" in response.headers["content-disposition"]
        assert str(tmp_path) not in response.url.path

        isolated = client.get(exported.download_url, headers={"X-Workspace-ID": "other"})
        assert isolated.status_code == 404
        assert client.get("/api/files/not-a-file-id").status_code == 404
