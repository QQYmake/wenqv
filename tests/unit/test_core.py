from __future__ import annotations

import asyncio
import json

from server.agent import (
    AgentConfig,
    AgentCore,
    InMemoryConversationStore,
    LLMClientFactory,
    LLMResponse,
    LLMStreamChunk,
    SkillManager,
    Tool,
    ToolCallDelta,
    ToolRegistry,
)


def tool_call(call_id: str, name: str, arguments: dict) -> list[LLMStreamChunk]:
    raw = json.dumps(arguments)
    midpoint = max(1, len(raw) // 2)
    return [
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(
                    index=0,
                    id=call_id,
                    name=name,
                    arguments_delta=raw[:midpoint],
                ),
            )
        ),
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, arguments_delta=raw[midpoint:]),
            ),
            finish_reason="tool_calls",
        ),
    ]


def text_response(*parts: str) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(
            content_delta=part,
            finish_reason="stop" if index == len(parts) - 1 else None,
        )
        for index, part in enumerate(parts)
    ]


class ScriptedClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.stream_calls = []

    async def stream(self, messages, *, tools=None, max_tokens=None):
        self.stream_calls.append((tuple(messages), tools, max_tokens))
        if not self.scripts:
            raise AssertionError("No scripted LLM turn remains")
        script = self.scripts.pop(0)
        for chunk in script:
            await asyncio.sleep(0)
            yield chunk

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("Generated title")


def make_core(tmp_path, client, *, config=AgentConfig(), skill_dir=None):
    skills = SkillManager(skill_dir or (tmp_path / "skills"))
    store = InMemoryConversationStore()
    core = AgentCore(
        store=store,
        clients=LLMClientFactory(client),
        skills=skills,
        config=config,
        workspace_root=tmp_path,
    )
    return core, store


async def collect(stream):
    return [event async for event in stream]


def test_multistep_react_loop_streams_two_tools_and_final_answer(tmp_path) -> None:
    (tmp_path / "note.txt").write_text("blue lake", encoding="utf-8")
    client = ScriptedClient(
        [
            tool_call("calc-1", "calculator", {"expression": "(17 + 5) * 3"}),
            tool_call("read-1", "read_file", {"path": "note.txt"}),
            text_response("The result is ", "66 and the file says blue lake."),
        ]
    )
    core, store = make_core(tmp_path, client)

    events = asyncio.run(collect(core.stream("s1", "Calculate, then read the file.")))
    payloads = [event.to_dict() for event in events]

    assert [e["type"] for e in payloads].count("tool_call") == 2
    assert [e["type"] for e in payloads].count("tool_result") == 2
    assert [e["call_id"] for e in payloads if e["type"] == "tool_call"] == [
        "calc-1",
        "read-1",
    ]
    assert "".join(e["delta"] for e in payloads if e["type"] == "text_delta").endswith(
        "66 and the file says blue lake."
    )
    assert payloads[-1]["reason"] == "complete"

    messages = asyncio.run(store.list_messages("s1"))
    assert [message.role for message in messages] == [
        "user",
        "assistant",
        "tool",
        "assistant",
        "tool",
        "assistant",
    ]
    assert json.loads(messages[2].content)["value"] == 66


def test_tool_error_returns_to_model_and_does_not_crash_loop(tmp_path) -> None:
    client = ScriptedClient(
        [
            tool_call("bad-1", "missing_tool", {}),
            text_response("That tool is unavailable, so I changed strategy."),
        ]
    )
    core, _store = make_core(tmp_path, client)

    events = asyncio.run(collect(core.stream("s1", "Use a missing tool.")))
    payloads = [event.to_dict() for event in events]

    tool_result = next(e for e in payloads if e["type"] == "tool_result")
    assert tool_result["error"] is True
    assert "Unknown tool" in tool_result["content"]
    assert payloads[-1]["reason"] == "complete"
    # The second model call sees the normalized failure as a tool message.
    assert client.stream_calls[1][0][-1].role == "tool"
    assert "Unknown tool" in client.stream_calls[1][0][-1].content


def test_retry_limit_forces_a_no_tool_final_turn(tmp_path) -> None:
    client = ScriptedClient(
        [
            tool_call("bad-1", "missing_tool", {}),
            text_response("I cannot use that unavailable tool."),
        ]
    )
    core, _store = make_core(
        tmp_path,
        client,
        config=AgentConfig(max_turns=3, max_tool_retries=0),
    )

    events = asyncio.run(collect(core.stream("s1", "Try once.")))
    assert events[-1].to_dict()["reason"] == "tool_retry_limit"
    assert client.stream_calls[0][1]
    assert client.stream_calls[1][1] is None


def test_max_turns_stops_gracefully_with_explanation(tmp_path) -> None:
    client = ScriptedClient(
        [tool_call("calc-1", "calculator", {"expression": "1 + 1"})]
    )
    core, store = make_core(
        tmp_path,
        client,
        config=AgentConfig(max_turns=1),
    )

    events = asyncio.run(collect(core.stream("s1", "Keep calculating.")))
    payloads = [event.to_dict() for event in events]
    assert any(e.get("code") == "max_turns_reached" for e in payloads)
    assert payloads[-1]["reason"] == "max_turns"
    messages = asyncio.run(store.list_messages("s1"))
    assert "configured limit" in (messages[-1].content or "")


def test_explicit_skill_mentions_persist_and_deduplicate_across_runs(tmp_path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "coach.md").write_text(
        "---\nname: coach\ndescription: Coaching workflow\n---\nAsk one useful question.\n",
        encoding="utf-8",
    )
    client = ScriptedClient([text_response("First."), text_response("Second.")])
    core, store = make_core(tmp_path, client, skill_dir=skill_dir)

    first = asyncio.run(collect(core.stream("s1", "@coach help me")))
    second = asyncio.run(collect(core.stream("s1", "@coach continue")))

    assert next(e for e in first if e.type == "skill_loaded").data["status"] == "loaded"
    assert next(e for e in second if e.type == "skill_loaded").data["status"] == "already_loaded"
    messages = asyncio.run(store.list_messages("s1"))
    injections = [m for m in messages if m.metadata.get("kind") == "skill_injection"]
    assert len(injections) == 1
    assert asyncio.run(store.list_session_skills("s1")) == {"coach"}


def test_load_skill_meta_tool_injects_once_and_reports_repeat(tmp_path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "coach.md").write_text(
        "---\nname: coach\ndescription: Coaching workflow\n---\nKeep the next step small.\n",
        encoding="utf-8",
    )
    client = ScriptedClient(
        [
            tool_call("load-1", "load_skill", {"name": "coach"}),
            tool_call("load-2", "load_skill", {"name": "coach"}),
            text_response("The skill is active."),
        ]
    )
    core, store = make_core(tmp_path, client, skill_dir=skill_dir)

    events = asyncio.run(collect(core.stream("s1", "Load what helps.")))
    payloads = [event.to_dict() for event in events]
    results = [e for e in payloads if e["type"] == "tool_result"]
    assert "skill_context" in results[0]["content"]
    assert "already_loaded" in results[1]["content"]
    assert sum(e["type"] == "skill_loaded" for e in payloads) == 1
    messages = asyncio.run(store.list_messages("s1"))
    assert sum(m.metadata.get("kind") == "skill_injection" for m in messages) == 1


def test_remove_skill_meta_tool_deactivates_context(tmp_path) -> None:
    skill_dir = tmp_path / "skills"
    skill_dir.mkdir()
    (skill_dir / "coach.md").write_text(
        "---\nname: coach\ndescription: Coaching workflow\n---\nKeep scope small.\n",
        encoding="utf-8",
    )
    client = ScriptedClient(
        [
            tool_call("load-1", "load_skill", {"name": "coach"}),
            tool_call("remove-1", "remove_skill", {"name": "coach"}),
            text_response("Removed."),
        ]
    )
    core, store = make_core(tmp_path, client, skill_dir=skill_dir)

    events = asyncio.run(collect(core.stream("s1", "Manage the skill.")))
    assert events[-1].data["reason"] == "complete"
    assert asyncio.run(store.list_session_skills("s1")) == set()
    messages = asyncio.run(store.list_messages("s1"))
    assert all(
        m.metadata.get("kind") != "skill_injection"
        for m in messages
    )
    assert any(m.metadata.get("kind") == "skill_removed" for m in messages)


def test_abort_interrupts_a_blocked_model_stream(tmp_path) -> None:
    class BlockingClient:
        def __init__(self):
            self.started = asyncio.Event()
            self.cancelled = asyncio.Event()

        async def stream(self, messages, *, tools=None, max_tokens=None):
            self.started.set()
            try:
                await asyncio.Future()
            finally:
                self.cancelled.set()
            if False:
                yield LLMStreamChunk()

        async def complete(self, messages, *, tools=None, max_tokens=None):
            return LLMResponse("")

    async def scenario() -> None:
        client = BlockingClient()
        core, _store = make_core(tmp_path, client)
        task = asyncio.create_task(collect(core.stream("s1", "Wait." , request_id="r1")))
        await asyncio.wait_for(client.started.wait(), timeout=1)
        assert await core.abort("s1", "r1")
        events = await asyncio.wait_for(task, timeout=1)
        assert events[-1].to_dict()["reason"] == "aborted"
        assert client.cancelled.is_set()
        assert not await core.abort("s1", "r1")

    asyncio.run(scenario())


def test_abort_interrupts_an_in_flight_tool(tmp_path) -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def block(_arguments, _context):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        client = ScriptedClient([tool_call("block-1", "block", {})])
        skills = SkillManager(tmp_path / "skills")
        core = AgentCore(
            store=InMemoryConversationStore(),
            clients=LLMClientFactory(client),
            skills=skills,
            tools=ToolRegistry(
                [
                    Tool(
                        "block",
                        "Block until cancellation",
                        {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": False,
                        },
                        block,
                    )
                ]
            ),
            workspace_root=tmp_path,
        )
        task = asyncio.create_task(
            collect(core.stream("s1", "Run the tool.", request_id="tool-run"))
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        assert await core.abort("s1", "tool-run")
        events = await asyncio.wait_for(task, timeout=1)
        assert events[-1].to_dict()["reason"] == "aborted"
        assert cancelled.is_set()

    asyncio.run(scenario())
