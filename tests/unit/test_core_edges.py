"""Edge cases of the agent loop: malformed tool arguments and broken model streams.

Each scenario must terminate with exactly one terminal ``done`` event and must
never crash the loop or leak an active run.
"""

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
    ToolCallDelta,
)


def _chunk_tool_call(call_id: str, name: str, arguments_raw: str) -> list[LLMStreamChunk]:
    return [
        LLMStreamChunk(
            tool_call_deltas=(
                ToolCallDelta(index=0, id=call_id, name=name, arguments_delta=arguments_raw),
            )
        ),
        LLMStreamChunk(tool_call_deltas=(ToolCallDelta(index=0),), finish_reason="tool_calls"),
    ]


class ScriptedClient:
    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.stream_calls = 0

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.stream_calls += 1
        if not self.scripts:
            raise AssertionError("No scripted LLM turn remains")
        script = self.scripts.pop(0)
        async def iterate():
            for chunk in script:
                yield chunk
        return iterate()

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("Generated title")


def make_core(tmp_path, client, *, config=AgentConfig()):
    skills = SkillManager(tmp_path / "skills")
    store = InMemoryConversationStore()
    core = AgentCore(
        store=store,
        clients=LLMClientFactory(client),
        skills=skills,
        config=config,
        workspace_root=tmp_path,
    )
    return core


async def collect(core, message="Run it.", session_id="s1"):
    return [event.to_dict() async for event in core.stream(session_id, message, request_id="edge-run")]


def test_malformed_json_tool_arguments_return_error_and_loop_continues(tmp_path) -> None:
    client = ScriptedClient(
        [
            _chunk_tool_call("bad-1", "calculator", '{"expression": "1 +'),
            [LLMStreamChunk(content_delta="The model recovered.")],
        ]
    )
    core = make_core(tmp_path, client)

    payloads = asyncio.run(collect(core))
    tool_result = next(e for e in payloads if e["type"] == "tool_result")
    assert tool_result["error"] is True
    assert "malformed JSON" in tool_result["content"]
    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["reason"] == "complete"
    assert sum(e["type"] == "done" for e in payloads) == 1


def test_non_object_json_tool_arguments_return_error_and_loop_continues(tmp_path) -> None:
    client = ScriptedClient(
        [
            _chunk_tool_call("arr-1", "calculator", '[1, 2, 3]'),
            [LLMStreamChunk(content_delta="Fixed it.")],
        ]
    )
    core = make_core(tmp_path, client)

    payloads = asyncio.run(collect(core))
    tool_result = next(e for e in payloads if e["type"] == "tool_result")
    assert tool_result["error"] is True
    assert payloads[-1]["reason"] == "complete"


def test_empty_model_stream_yields_error_and_single_done(tmp_path) -> None:
    """A model stream that ends without any chunk must not hang or crash."""
    client = ScriptedClient([[]])
    core = make_core(tmp_path, client)

    payloads = asyncio.run(collect(core))
    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["reason"] == "error"
    assert sum(e["type"] == "done" for e in payloads) == 1
    errors = [e for e in payloads if e["type"] == "error"]
    assert errors and errors[0]["code"] == "empty_model_response"


class ExplodingClient(ScriptedClient):
    def __init__(self, scripts, *, fail_first=True, message="upstream timeout"):
        super().__init__(scripts)
        self.fail_first = fail_first
        self.message = message

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.stream_calls += 1
        if self.fail_first:
            self.fail_first = False
            raise RuntimeError(self.message)
        script = self.scripts.pop(0)

        async def iterate():
            for chunk in script:
                yield chunk

        return iterate()


def test_model_stream_raising_propagates_as_error_then_done(tmp_path) -> None:
    core = make_core(
        tmp_path,
        ExplodingClient(
            [[]], message="upstream timeout"
        ),
    )

    payloads = asyncio.run(collect(core))
    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["reason"] == "error"
    assert sum(e["type"] == "done" for e in payloads) == 1
    errors = [e for e in payloads if e["type"] == "error"]
    assert errors and "upstream timeout" in errors[0]["message"]


def test_model_stream_yielding_wrong_type_is_reported_as_error(tmp_path) -> None:
    class WrongTypeClient(ScriptedClient):
        async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
            yield {"type": "text_delta", "delta": "not an LLMStreamChunk"}  # type: ignore[misc]

    core = make_core(tmp_path, WrongTypeClient([[]]))

    payloads = asyncio.run(collect(core))
    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["reason"] == "error"
    assert sum(e["type"] == "done" for e in payloads) == 1
    errors = [e for e in payloads if e["type"] == "error"]
    assert errors and "LLMStreamChunk" in errors[0]["message"]


def test_active_run_is_cleaned_up_after_edge_failure(tmp_path) -> None:
    """After a broken model turn the session must be reusable for a new run."""
    client = ExplodingClient([[]], message="down")
    core = make_core(tmp_path, client)
    assert asyncio.run(collect(core))[-1]["reason"] == "error"

    client.scripts = [[LLMStreamChunk(content_delta="back online.")]]
    payloads = asyncio.run(collect(core, session_id="s1"))
    assert payloads[-1]["reason"] == "complete"
    assert "".join(e["delta"] for e in payloads if e["type"] == "text_delta") == "back online."


def test_tool_retry_limit_does_not_loop_forever_on_invalid_arguments(tmp_path) -> None:
    """Repeated malformed tool arguments must terminate via the retry limit."""
    client = ScriptedClient(
        [
            _chunk_tool_call("bad-1", "calculator", "not json at all"),
            _chunk_tool_call("bad-2", "calculator", "not json at all"),
            _chunk_tool_call("bad-3", "calculator", "not json at all"),
        ]
    )
    core = make_core(
        tmp_path,
        client,
        config=AgentConfig(max_turns=5, max_tool_retries=1),
    )

    payloads = asyncio.run(collect(core))
    assert payloads[-1]["type"] == "done"
    assert payloads[-1]["reason"] == "tool_retry_limit"
    assert sum(e["type"] == "done" for e in payloads) == 1
    # Turns: tool error 1, tool error 2 (retry limit hit), forced final turn.
    assert client.stream_calls == 3
    assert client.scripts == []  # the loop stopped; no unbounded retry
