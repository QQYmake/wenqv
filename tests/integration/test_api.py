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
    load_config,
)
from server.agent.context import ContextConfig
from server.agent.core import AgentConfig, AgentCore
from server.agent.models import LLMStreamChunk, ToolCallDelta
from server.agent.skills import SkillManager
from server.main import create_app
from server.api.services import normalize_event
from server.storage import AgentStoreAdapter, SQLiteStore


class FakeAgent:
    persists_messages = False

    def __init__(self):
        self.abort_calls: list[tuple[str, str | None]] = []

    async def stream(self, **kwargs):
        yield {"type": "tool_call", "call_id": "c1", "name": "calculator", "arguments": {"expression": "2+2"}}
        yield {"type": "tool_result", "call_id": "c1", "name": "calculator", "result": "4", "error": False}
        yield {"type": "text_delta", "delta": "The answer "}
        yield {"type": "text_delta", "delta": "is 4."}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, session_id: str, request_id: str | None = None):
        self.abort_calls.append((session_id, request_id))
        return True


class FakeSkills:
    def catalog(self):
        return [{"name": "demo", "description": "Demo workflow"}]


class FakeTitleGenerator:
    def __init__(self):
        self.calls = 0

    async def generate_title(self, messages, workspace_id=None):
        self.calls += 1
        return "Simple Calculation"


class TwoToolClient:
    def __init__(self):
        self.turn = 0

    async def stream(self, messages, *, tools=None, max_tokens=None):
        self.turn += 1
        if self.turn <= 2:
            expression = "2+2" if self.turn == 1 else "4*3"
            yield LLMStreamChunk(
                tool_call_deltas=(
                    ToolCallDelta(
                        index=0,
                        id=f"call-{self.turn}",
                        name="calculator",
                        arguments_delta='{"expression":"' + expression + '"}',
                    ),
                )
            )
        else:
            yield LLMStreamChunk(content_delta="The final answer is 12.")

    async def complete(self, messages, *, tools=None, max_tokens=None):
        raise AssertionError("The injected title generator handles background titles")


class FakeClients:
    def __init__(self, client):
        self.client = client

    def get_client(self, role, workspace_id=None):
        return self.client


def make_config() -> AppConfig:
    return AppConfig(
        llm=LLMSettings(
            main=LLMProviderConfig(
                base_url="https://example.invalid/v1", api_key="secret", model="fake-main"
            )
        ),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(static_dir=Path("missing-dist")),
        workspace=WorkspaceSettings(default_id="default", root=Path.cwd()),
    )


def event_payloads(response) -> list[dict]:
    import json

    return [
        json.loads(line.removeprefix("data: "))
        for line in response.iter_lines()
        if line.startswith("data: ")
    ]


def test_session_crud_and_workspace_isolation():
    app = create_app(make_config(), agent=FakeAgent())
    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        created = client.post("/api/sessions", json={"title": "Planning"})
        assert created.status_code == 201
        session = created.json()
        assert client.get("/api/sessions").json()["sessions"][0]["id"] == session["id"]
        assert client.get(
            f"/api/sessions/{session['id']}/messages",
            headers={"X-Workspace-ID": "another"},
        ).status_code == 404

        renamed = client.patch(
            f"/api/sessions/{session['id']}", json={"title": "Revised"}
        )
        assert renamed.json()["title"] == "Revised"
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 204
        assert client.delete(f"/api/sessions/{session['id']}").status_code == 404


def test_chat_sse_has_named_events_and_persists_history():
    title_generator = FakeTitleGenerator()
    app = create_app(
        make_config(),
        agent=FakeAgent(),
        skill_manager=FakeSkills(),
        title_generator=title_generator,
    )
    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "Please calculate 2+2"},
        ) as response:
            assert response.status_code == 200
            events = event_payloads(response)

        assert [event["type"] for event in events] == [
            "tool_call",
            "tool_result",
            "text_delta",
            "text_delta",
            "done",
        ]
        assert events[-1]["session_id"] == session["id"]
        messages = client.get(f"/api/sessions/{session['id']}/messages").json()[
            "messages"
        ]
        assert [message["kind"] for message in messages] == [
            "message",
            "tool_call",
            "tool_result",
            "message",
        ]
        assert messages[-1]["content"] == "The answer is 4."
        assert client.get("/api/sessions").json()["sessions"][0]["title"] == "Simple Calculation"
        assert title_generator.calls == 1


def test_skills_config_and_abort_are_safe():
    agent = FakeAgent()
    app = create_app(make_config(), agent=agent, skill_manager=FakeSkills())
    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        session = client.post("/api/sessions", json={}).json()
        assert client.get("/api/skills").json() == {
            "skills": [{"name": "demo", "description": "Demo workflow"}]
        }
        public = client.get("/api/config").json()
        assert public["model_id"] == "fake-main"
        assert "api_key" not in str(public)

        aborted = client.post(
            "/api/chat/abort", json={"session_id": session["id"]}
        )
        assert aborted.status_code == 200
        assert aborted.json()["aborted"] is True
        assert agent.abort_calls == [(session["id"], None)]


def test_project_config_aliases_and_environment_overrides_are_resolved():
    config = load_config(
        "config.yaml",
        environ={
            "AGENT_MAIN_API_KEY": "do-not-expose",
            "AGENT_MAIN_MODEL": "override-main",
            "AGENT_SUMMARY_MODEL": "small-summary",
            "REDIS_URL": "redis://localhost:6379/5",
        },
    )
    assert config.workspace.id == "default"
    assert config.workspace.root == Path.cwd().resolve()
    assert config.agent.tool_result_max_chars == 24_000
    assert config.context.preserve_recent_messages == 10
    assert config.llm.summary is not None
    assert config.llm.summary.base_url == config.llm.main.base_url
    assert config.llm.summary.api_key == config.llm.main.api_key
    assert config.storage.redis_url == "redis://localhost:6379/5"
    assert "do-not-expose" not in str(config.public_dict())


def test_core_event_aliases_are_normalized_for_the_browser():
    result = normalize_event(
        {
            "type": "tool_result",
            "tool_call_id": "call-7",
            "content": "42",
        }
    )
    assert result["call_id"] == "call-7"
    assert result["result"] == "42"
    assert normalize_event({"type": "done", "reason": "max_turns"})[
        "finish_reason"
    ] == "max_turns"
    assert normalize_event(
        {"type": "skill_loaded", "name": "demo", "status": "already_loaded"}
    )["already_loaded"] is True


def test_real_agent_core_completes_two_tool_turns_over_sse():
    raw_store = SQLiteStore(":memory:")
    agent_store = AgentStoreAdapter(raw_store)
    skills = SkillManager(Path("missing-test-skills"))
    core = AgentCore(
        store=agent_store,
        clients=FakeClients(TwoToolClient()),
        skills=skills,
        config=AgentConfig(max_turns=6),
        context_config=ContextConfig(token_budget=4_000),
        workspace_root=Path.cwd(),
    )
    app = create_app(
        make_config(),
        store=raw_store,
        agent=core,
        skill_manager=skills,
        title_generator=FakeTitleGenerator(),
    )
    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "Calculate in two steps"},
        ) as response:
            events = event_payloads(response)
        assert sum(event["type"] == "tool_call" for event in events) == 2
        assert sum(event["type"] == "tool_result" for event in events) == 2
        assert events[-1]["type"] == "done"
        assert events[-1]["finish_reason"] == "complete"
        messages = client.get(f"/api/sessions/{session['id']}/messages").json()[
            "messages"
        ]
        assert messages[-1]["content"] == "The final answer is 12."
        assert [message["kind"] for message in messages].count("tool_call") == 2
        assert [message["kind"] for message in messages].count("tool_result") == 2
