from __future__ import annotations

import asyncio
from pathlib import Path

from server.agent import (
    AgentConfig,
    AgentCore,
    LLMClientFactory,
    LLMResponse,
    LLMStreamChunk,
    SkillManager,
)
from server.storage import AgentStoreAdapter, SQLiteStore


class RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.calls.append(tuple(messages))
        yield LLMStreamChunk(content_delta="ready", finish_reason="stop")

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("title")


def test_default_wenqu_isolated_by_conversation_and_archive_survives_delete(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        raw = SQLiteStore(":memory:")
        await raw.initialize()
        first = await raw.create_session("browser-a", session_id="conversation-a")
        second = await raw.create_session("browser-a", session_id="conversation-b")
        workspace = tmp_path / "workspace"
        training = workspace / "wenqu" / "sessions" / "training-a"
        training.mkdir(parents=True)
        archive = training / "current.md"
        archive.write_text(
            "training_id: training-a\nowner_conversation_id: conversation-a\n",
            encoding="utf-8",
        )

        client = RecordingClient()
        skills_root = Path(__file__).resolve().parents[2] / "skills"
        store = AgentStoreAdapter(raw)
        core = AgentCore(
            store=store,
            clients=LLMClientFactory(client),
            skills=SkillManager(skills_root),
            config=AgentConfig(default_skills=("wenqu",)),
            workspace_root=workspace,
        )

        first_events = [
            event.to_dict()
            async for event in core.stream(first["id"], "开始备课")
        ]
        second_events = [
            event.to_dict()
            async for event in core.stream(second["id"], "开始另一项备课")
        ]

        assert next(e for e in first_events if e["type"] == "skill_loaded") == {
            "type": "skill_loaded",
            "name": "wenqu",
            "status": "loaded",
            "source": "default",
            "request_id": first_events[0]["request_id"],
        }
        assert next(e for e in second_events if e["type"] == "skill_loaded")[
            "status"
        ] == "loaded"
        assert {item["skill_name"] for item in await raw.list_loaded_skills(first["id"])} == {
            "wenqu"
        }
        assert {item["skill_name"] for item in await raw.list_loaded_skills(second["id"])} == {
            "wenqu"
        }

        first_context = next(
            message for message in client.calls[0] if message.metadata.get("kind") == "skill_injection"
        ).content
        second_context = next(
            message for message in client.calls[1] if message.metadata.get("kind") == "skill_injection"
        ).content
        assert 'conversation_id: "conversation-a"' in first_context
        assert 'conversation_id: "conversation-b"' in second_context
        assert "conversation-b" not in first_context

        assert await raw.delete_session(first["id"], "browser-a")
        assert archive.read_text(encoding="utf-8").startswith("training_id: training-a")
        await raw.close()

    asyncio.run(scenario())
