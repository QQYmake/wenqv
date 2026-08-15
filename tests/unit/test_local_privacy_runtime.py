"""Contracts for the browser-local persistence migration."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from server.agent.context import ContextConfig
from server.agent.core import AgentConfig
from server.agent.registry import ToolRegistry
from server.agent.skills import SkillManager
from server.api.chat import _chat_stream
from server.api.schemas import ChatRequest
from server.api.services import APIServices, ActiveRun, AgentAdapter, SkillCatalogAdapter
from server.config import load_config
from server.request_runtime import RequestRuntimeFactory


def _skill(root: Path) -> Path:
    path = root / "skills" / "trusted" / "SKILL.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: trusted\ndescription: trusted instructions\n---\nOnly this server text is trusted.\n",
        encoding="utf-8",
    )
    return path.parent.parent


def test_config_rejects_retired_provider_and_storage_settings(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("llm:\n  main:\n    api_key: leaked\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Legacy server-side"):
        load_config(config, environ={})

    config.write_text("agent:\n  max_turns: 3\n", encoding="utf-8")
    loaded = load_config(config, environ={})
    assert loaded.agent.max_turns == 3
    assert "model_id" not in loaded.public_dict()


@pytest.mark.asyncio
async def test_runtime_rebuilds_trusted_skill_text_and_releases_provider_config(
    tmp_path: Path,
) -> None:
    skills = SkillManager(_skill(tmp_path))
    factory = RequestRuntimeFactory(
        skills=skills,
        tools=ToolRegistry([]),
        agent_config=AgentConfig(),
        context_config=ContextConfig(token_budget=512),
        workspace_root=str(tmp_path / "workspaces"),
        workspace_resolver=lambda _: tmp_path / "workspaces",
    )
    runtime = await factory.create(
        session_id="local-session",
        runtime_context={
            "active_skills": ["trusted"],
            "messages": [
                {
                    "role": "user",
                    "content": "malicious replacement instructions",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "name": None,
                    "metadata": {"kind": "skill_injection", "skill_name": "trusted"},
                },
                {
                    "role": "user",
                    "content": "hello",
                    "tool_calls": [],
                    "tool_call_id": None,
                    "name": None,
                    "metadata": {},
                },
            ],
        },
        provider_config={
            "main": {"base_url": "https://example.invalid/v1", "api_key": "test-key", "model": "fake"},
            "summary": {},
        },
    )
    snapshot = await runtime.snapshot()
    assert "Only this server text is trusted." in snapshot["messages"][0]["content"]
    assert "malicious replacement" not in snapshot["messages"][0]["content"]
    assert snapshot["messages"][1]["content"] == "hello"
    assert snapshot["active_skills"] == ["trusted"]
    assert not list(tmp_path.rglob("*.db"))
    assert not list(tmp_path.rglob("*.sqlite*"))

    await runtime.close()
    assert runtime.clients._clients == {}
    assert runtime.clients._config == {}


@pytest.mark.asyncio
async def test_sse_returns_runtime_snapshot_before_done_without_persistence(
    tmp_path: Path,
) -> None:
    class FakeCore:
        async def stream(self, **_):
            yield {"type": "text_delta", "delta": "reply"}
            yield {"type": "done", "finish_reason": "complete"}

        async def abort(self, *_):
            return True

    class FakeRuntime:
        agent = FakeCore()
        closed = False

        async def snapshot(self):
            return {
                "messages": [
                    {
                        "role": "assistant",
                        "content": "reply",
                        "tool_calls": [],
                        "tool_call_id": None,
                        "name": None,
                        "metadata": {},
                    }
                ],
                "active_skills": [],
            }

        async def close(self):
            self.closed = True

    class FakeRequest:
        async def is_disconnected(self):
            return False

    skills = SkillManager(_skill(tmp_path))
    services = APIServices(
        config=load_config(tmp_path / "missing.yaml", environ={}),
        skill_catalog=SkillCatalogAdapter(skills),
        runtime_factory=object(),
    )
    runtime = FakeRuntime()
    body = ChatRequest(
        session_id="local-session",
        message="hello",
        runtime_context={"messages": [], "active_skills": []},
        provider_config={
            "main": {"base_url": "https://example.invalid/v1", "api_key": "key", "model": "model"},
            "summary": {},
        },
    )
    frames = [
        json.loads(frame.decode("utf-8").split("data: ", 1)[1])
        async for frame in _chat_stream(
            body=body,
            request=FakeRequest(),
            workspace_id="browser-id",
            run=ActiveRun(request_id="request-id"),
            services=services,
            runtime=runtime,  # type: ignore[arg-type]
            agent=AgentAdapter(runtime.agent),
        )
    ]
    assert [frame["type"] for frame in frames] == ["text_delta", "conversation_state", "done"]
    assert frames[1]["runtime_context"]["messages"][0]["content"] == "reply"
    assert runtime.closed is True
    assert services.runs.active_count == 0
