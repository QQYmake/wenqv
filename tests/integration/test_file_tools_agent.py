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


class FileToolClient:
    def __init__(self) -> None:
        self.turn = 0

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.turn += 1
        if self.turn == 1:
            calls = [
                ("write-1", "write", {"path": "notes/note.txt", "content": "alpha\n"}),
                (
                    "edit-1",
                    "edit",
                    {
                        "path": "notes/note.txt",
                        "edits": [{"oldText": "alpha", "newText": "beta"}],
                    },
                ),
                ("read-1", "read", {"path": "notes/note.txt"}),
                ("grep-1", "grep", {"pattern": "beta", "path": "notes"}),
                ("find-1", "find", {"pattern": "**/*.txt"}),
                ("ls-1", "ls", {"path": "notes"}),
            ]
            yield LLMStreamChunk(
                tool_call_deltas=tuple(
                    ToolCallDelta(
                        index=index,
                        id=call_id,
                        name=name,
                        arguments_delta=json.dumps(arguments),
                    )
                    for index, (call_id, name, arguments) in enumerate(calls)
                ),
                finish_reason="tool_calls",
            )
            return
        yield LLMStreamChunk(content_delta="All file operations completed.", finish_reason="stop")

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("title")


def test_real_agent_core_runs_all_global_file_tools_and_persists_edit_patch(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        raw = SQLiteStore(":memory:")
        await raw.initialize()
        session = await raw.create_session("default")
        store = AgentStoreAdapter(raw)
        core = AgentCore(
            store=store,
            clients=LLMClientFactory(FileToolClient()),
            skills=SkillManager(tmp_path / "skills"),
            workspace_root=tmp_path,
        )

        events = [
            event.to_dict()
            async for event in core.stream(session["id"], "Exercise every file tool")
        ]
        assert [event["name"] for event in events if event["type"] == "tool_call"] == [
            "write",
            "edit",
            "read",
            "grep",
            "find",
            "ls",
        ]
        results = [event for event in events if event["type"] == "tool_result"]
        assert len(results) == 6
        assert all(not event["error"] for event in results)
        assert next(event for event in results if event["name"] == "edit")["patch"].startswith(
            "--- a/notes/note.txt"
        )
        assert (tmp_path / "notes" / "note.txt").read_text(encoding="utf-8") == "beta\n"

        messages = await store.list_messages(session["id"])
        edit_message = next(
            message
            for message in messages
            if message.role == "tool" and message.name == "edit"
        )
        assert edit_message.metadata["ui_patch"].startswith("--- a/notes/note.txt")
        assert messages[-1].content == "All file operations completed."
        await raw.close()

    asyncio.run(scenario())
