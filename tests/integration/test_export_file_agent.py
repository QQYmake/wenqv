from __future__ import annotations

import asyncio
import json
from pathlib import Path

from server.agent import (
    AgentCore,
    LLMClientFactory,
    LLMResponse,
    LLMStreamChunk,
    SkillManager,
    ToolCallDelta,
)
from server.storage import AgentStoreAdapter, SQLiteStore


class ExportClient:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.turn += 1
        if self.turn == 1:
            yield LLMStreamChunk(
                tool_call_deltas=(
                    ToolCallDelta(
                        index=0,
                        id="export-1",
                        name="export_file",
                        arguments_delta=json.dumps(
                            {
                                "filename": "agent-report",
                                "format": "md",
                                "content": "# Agent report\n\n中文内容",
                            },
                            ensure_ascii=False,
                        ),
                    ),
                ),
                finish_reason="tool_calls",
            )
            return
        yield LLMStreamChunk(content_delta="The report is ready.", finish_reason="stop")

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("title")


def test_agent_can_call_export_file_and_return_download_metadata(tmp_path: Path) -> None:
    async def scenario() -> None:
        raw_store = SQLiteStore(":memory:")
        await raw_store.initialize()
        session = await raw_store.create_session("default")
        store = AgentStoreAdapter(raw_store)
        core = AgentCore(
            store=store,
            clients=LLMClientFactory(ExportClient()),
            skills=SkillManager(tmp_path / "skills"),
            workspace_root=tmp_path,
        )

        events = [
            event.to_dict()
            async for event in core.stream(session["id"], "Export this report")
        ]
        result = next(event for event in events if event["type"] == "tool_result")
        assert result["name"] == "export_file"
        assert result["error"] is False
        assert result["result"]["file_id"] == result["result"]["download_url"].rsplit("/", 1)[-1]
        assert result["result"]["filename"] == "agent-report.md"
        assert result["result"]["download_url"].startswith("/api/files/")
        assert (tmp_path / ".agent-exports").is_dir()
        assert events[-1]["type"] == "done"
        await raw_store.close()

    asyncio.run(scenario())
