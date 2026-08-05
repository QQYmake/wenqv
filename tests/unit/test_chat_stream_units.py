"""Unit tests for the SSE transport: frame encoding and _chat_stream control flow.

These tests drive ``server.api.chat._chat_stream`` directly with fakes so the
wire-level guarantees are verified deterministically: one complete SSE frame
per yielded item, a single terminal ``done`` event on every path, cooperative
abort on client disconnect, and no events after an abort.
"""

from __future__ import annotations

import json

import pytest

from server.api.chat import _chat_stream, encode_sse
from server.api.schemas import ChatRequest
from server.api.services import ActiveRun


def _body(**overrides) -> ChatRequest:
    values = {"session_id": "s1", "message": "hi", "request_id": "r1"}
    values.update(overrides)
    return ChatRequest(**values)


class FakeRequest:
    def __init__(self, disconnected_after: int | None = None):
        self._calls = 0
        self._disconnected_after = disconnected_after

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._disconnected_after is not None and self._calls > self._disconnected_after


class FakeAgent:
    persists_messages = False

    def __init__(self, events, *, raise_at=None, exc=RuntimeError("boom")):
        self.events = list(events)
        self.raise_at = raise_at
        self.exc = exc
        self.abort_calls: list[tuple[str, str | None]] = []

    async def stream(self, **kwargs):
        for index, event in enumerate(self.events):
            if self.raise_at == index:
                raise self.exc
            yield event

    async def abort(self, session_id: str, request_id: str | None = None):
        self.abort_calls.append((session_id, request_id))
        return True


class FakeStore:
    def __init__(self):
        self.added: list[tuple] = []
        self.tool_events: list[tuple] = []
        self.renamed: list[tuple] = []

    async def add_message(self, *args, **kwargs):
        self.added.append((args, kwargs))

    async def add_tool_event(self, *args, **kwargs):
        self.tool_events.append((args, kwargs))

    async def rename_session(self, *args, **kwargs):
        self.renamed.append((args, kwargs))


class FakeRuns:
    def __init__(self):
        self.finished: list[tuple] = []

    async def finish(self, workspace_id, session_id, request_id):
        self.finished.append((workspace_id, session_id, request_id))


def _services(agent: FakeAgent, store: FakeStore | None = None):
    return type(
        "Services",
        (),
        {
            "agent": agent,
            "store": store or FakeStore(),
            "runs": FakeRuns(),
            "title_generator": None,
            "agent_store": None,
        },
    )()


async def _collect(stream):
    return [frame async for frame in stream]


def test_encode_sse_frame_format() -> None:
    frame = encode_sse({"type": "text_delta", "delta": "湖畔", "n": 1, "obj": {"a": 1}})
    text = frame.decode("utf-8")
    assert text.startswith("event: text_delta\ndata: ")
    assert text.endswith("\n\n")
    # ensure_ascii=False: Chinese content must be sent raw, not \u escaped.
    assert "湖畔" in text
    assert "\\u6e56" not in text
    payload = json.loads(text.split("data: ", 1)[1].strip())
    assert payload == {"type": "text_delta", "delta": "湖畔", "n": 1, "obj": {"a": 1}}


def test_encode_sse_defaults_event_name_and_serializes_unknown_values() -> None:
    frame = encode_sse({"delta": "x", "when": object()})
    payload = json.loads(frame.decode("utf-8").split("data: ", 1)[1].strip())
    assert frame.decode("utf-8").startswith("event: message\n")
    # The payload is forwarded verbatim; the default "message" is the wire name.
    assert "type" not in payload
    assert payload["when"].startswith("<object")


def test_chat_stream_yields_one_complete_frame_per_event() -> None:
    agent = FakeAgent(
        [
            {"type": "text_delta", "delta": "a"},
            {"type": "tool_call", "call_id": "c1", "name": "calculator", "arguments": {}},
            {"type": "tool_result", "call_id": "c1", "result": "4"},
            {"type": "done", "finish_reason": "stop"},
        ]
    )
    services = _services(agent)
    frames = []
    async def scenario():
        nonlocal frames
        frames = await _collect(
            _chat_stream(
                body=_body(),
                request=FakeRequest(),
                workspace_id="ws",
                initial_session={"message_count": 1, "title": "Existing"},
                run=ActiveRun(request_id="r1"),
                services=services,
            )
        )
    import asyncio

    asyncio.run(scenario())

    assert len(frames) == 4  # one frame per event, no extra frames
    types = []
    for frame in frames:
        text = frame.decode("utf-8")
        assert text.count("\n\n") == 1, f"frame must contain exactly one SSE boundary: {text!r}"
        event_line, data_line = text.split("\n", 1)
        types.append(event_line.removeprefix("event: "))
        assert data_line.startswith("data: ")
    assert types == ["text_delta", "tool_call", "tool_result", "done"]
    assert sum(t == "done" for t in types) == 1
    assert services.runs.finished == [("ws", "s1", "r1")]


def test_chat_stream_model_error_yields_error_then_single_done() -> None:
    agent = FakeAgent(
        [
            {"type": "text_delta", "delta": "partial"},
            {"type": "done", "finish_reason": "stop"},
        ],
        raise_at=1,
        exc=RuntimeError("upstream model exploded"),
    )
    services = _services(agent)
    frames = []
    async def scenario():
        nonlocal frames
        frames = await _collect(
            _chat_stream(
                body=_body(),
                request=FakeRequest(),
                workspace_id="ws",
                initial_session={"message_count": 1, "title": "Existing"},
                run=ActiveRun(request_id="r1"),
                services=services,
            )
        )
    import asyncio

    asyncio.run(scenario())

    payloads = [
        json.loads(frame.decode("utf-8").split("data: ", 1)[1].strip()) for frame in frames
    ]
    assert [p["type"] for p in payloads] == ["text_delta", "error", "done"]
    assert payloads[1]["code"] == "chat_failed"
    assert "upstream model exploded" in payloads[1]["message"]
    done = payloads[-1]
    assert done["finish_reason"] == "error"
    assert done["session_id"] == "s1" and done["request_id"] == "r1"
    assert sum(p["type"] == "done" for p in payloads) == 1
    assert services.runs.finished == [("ws", "s1", "r1")]


def test_chat_stream_client_disconnect_aborts_and_stops() -> None:
    """A disconnected client must trigger cooperative abort and stop yielding."""
    agent = FakeAgent(
        [
            {"type": "text_delta", "delta": "first"},
            {"type": "text_delta", "delta": "second"},
            {"type": "done", "finish_reason": "stop"},
        ]
    )
    services = _services(agent)
    run = ActiveRun(request_id="r1")
    frames = []
    async def scenario():
        nonlocal frames
        frames = await _collect(
            _chat_stream(
                body=_body(),
                request=FakeRequest(disconnected_after=1),
                workspace_id="ws",
                initial_session={"message_count": 1, "title": "Existing"},
                run=run,
                services=services,
            )
        )
    import asyncio

    asyncio.run(scenario())

    # Only the first event is delivered; the stream stops without a done frame.
    payloads = [
        json.loads(frame.decode("utf-8").split("data: ", 1)[1].strip()) for frame in frames
    ]
    assert [p["type"] for p in payloads] == ["text_delta"]
    assert agent.abort_calls == [("s1", "r1")]
    assert run.abort_event.is_set()
    assert services.runs.finished == [("ws", "s1", "r1")]


def test_chat_stream_abort_event_terminates_with_single_done() -> None:
    """After /api/chat/abort sets the run event, no further events are sent."""
    agent = FakeAgent(
        [
            {"type": "text_delta", "delta": "first"},
            {"type": "text_delta", "delta": "second"},
            {"type": "text_delta", "delta": "third"},
        ]
    )
    services = _services(agent)
    run = ActiveRun(request_id="r1")

    async def scenario():
        frames = []
        async for frame in _chat_stream(
            body=_body(),
            request=FakeRequest(),
            workspace_id="ws",
            initial_session={"message_count": 1, "title": "Existing"},
            run=run,
            services=services,
        ):
            frames.append(frame)
            if len(frames) == 1:
                # Simulate the abort endpoint firing after the first frame.
                run.abort_event.set()
        return frames

    import asyncio

    frames = asyncio.run(scenario())
    payloads = [
        json.loads(frame.decode("utf-8").split("data: ", 1)[1].strip()) for frame in frames
    ]
    # The second and third text events are suppressed; exactly one terminal done.
    assert [p["type"] for p in payloads] == ["text_delta", "done"]
    assert payloads[-1]["finish_reason"] == "aborted"
    assert payloads[-1]["session_id"] == "s1"
    assert services.runs.finished == [("ws", "s1", "r1")]


@pytest.mark.parametrize(
    "finish_reason",
    ["stop", "complete", "max_turns", "tool_retry_limit", "aborted", "error"],
)
def test_chat_stream_agent_done_is_forwarded_exactly_once(finish_reason: str) -> None:
    """Whatever the agent's reason, its done event must be the only terminal."""
    agent = FakeAgent(
        [
            {"type": "text_delta", "delta": "x"},
            {"type": "done", "finish_reason": finish_reason},
        ]
    )
    services = _services(agent)
    frames = []
    async def scenario():
        nonlocal frames
        frames = await _collect(
            _chat_stream(
                body=_body(),
                request=FakeRequest(),
                workspace_id="ws",
                initial_session={"message_count": 1, "title": "Existing"},
                run=ActiveRun(request_id="r1"),
                services=services,
            )
        )
    import asyncio

    asyncio.run(scenario())
    payloads = [
        json.loads(frame.decode("utf-8").split("data: ", 1)[1].strip()) for frame in frames
    ]
    assert [p["type"] for p in payloads] == ["text_delta", "done"]
    assert payloads[-1]["finish_reason"] == finish_reason
