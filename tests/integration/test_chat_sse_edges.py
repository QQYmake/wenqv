"""HTTP-level SSE edge cases: abort, concurrency, errors, resume, isolation.

All models are fake (programmable async transports); nothing touches the
public network. Tests that only read a completed stream run on Starlette's
TestClient. Tests that must keep a stream open while issuing another request
(abort, 409 conflict, per-user isolation) run against a real uvicorn server on
a random loopback port, because httpx's ASGITransport (used by TestClient)
buffers the whole response body and would deadlock on concurrent requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
import time
from pathlib import Path
from typing import Iterator

import httpx
import uvicorn
from fastapi.testclient import TestClient

from server.agent import AgentCore, LLMClientFactory, LLMResponse, LLMStreamChunk, SkillManager
from server.agent.context import ContextConfig
from server.config import (
    AppConfig,
    LLMProviderConfig,
    LLMSettings,
    ServerSettings,
    StorageSettings,
    WorkspaceSettings,
)
from server.main import create_app
from server.storage import AgentStoreAdapter, SQLiteStore


def make_config(tmp_path: Path) -> AppConfig:
    return AppConfig(
        llm=LLMSettings(
            main=LLMProviderConfig(
                base_url="https://example.invalid/v1", api_key="secret", model="fake-main"
            )
        ),
        storage=StorageSettings(sqlite_path=Path(":memory:")),
        server=ServerSettings(
            static_dir=Path("missing-dist"),
            cors_origins=("http://localhost:5173",),
            cookie_secure=False,
        ),
        workspace=WorkspaceSettings(default_id="default", root=tmp_path / "ws"),
    )


@contextlib.contextmanager
def running_server(app) -> Iterator[str]:
    """Run the app on a random loopback port; yield its base URL."""
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while (not server.started or not server.servers) and time.monotonic() < deadline:
            time.sleep(0.02)
        if not server.started or not server.servers:
            raise RuntimeError("uvicorn server did not start")
        port = server.servers[0].sockets[0].getsockname()[1]
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


def next_sse_frame(lines) -> list[str]:
    """Consume one SSE frame (until the blank separator line) from an iterator.

    The separator line itself is consumed (returned frame never includes it);
    callers that later merge frames must re-insert a blank separator so the
    parser sees each frame boundary.
    """
    frame: list[str] = []
    for line in lines:
        if line == "":
            return frame
        frame.append(line)
    return frame


def sse_events(lines) -> list[dict]:
    """Parse SSE lines into (event, data) dictionaries; blank lines separate."""
    events: list[dict] = []
    current: dict[str, str] = {}
    for line in lines:
        if line == "":
            if current:
                events.append(current)
                current = {}
            continue
        if line.startswith("event: "):
            current["event"] = line.removeprefix("event: ")
        elif line.startswith("data: "):
            current["data"] = line.removeprefix("data: ")
    if current:
        events.append(current)
    return events


def event_payloads(events: list[dict]) -> list[dict]:
    return [json.loads(event["data"]) for event in events]


def test_model_error_mid_stream_yields_error_then_single_done(tmp_path: Path) -> None:
    class ExplodingAgent:
        persists_messages = False

        async def stream(self, **kwargs):
            yield {"type": "text_delta", "delta": "partial"}
            raise RuntimeError("model exploded mid-stream")

        async def abort(self, *_a, **_k):
            return False

    app = create_app(make_config(tmp_path), agent=ExplodingAgent())
    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client:
        session = client.post("/api/sessions", json={}).json()
        with client.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "hi"},
        ) as response:
            assert response.status_code == 200
            payloads = event_payloads(sse_events(response.iter_lines()))

        assert [p["type"] for p in payloads] == ["text_delta", "error", "done"]
        assert payloads[1]["code"] == "chat_failed"
        assert "model exploded" in payloads[1]["message"]
        assert payloads[-1]["finish_reason"] == "error"
        assert sum(p["type"] == "done" for p in payloads) == 1


class RecordingClient:
    """Records every messages list it receives; replays a scripted stream."""

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.calls: list[list] = []

    async def stream(self, messages, *, tools=None, max_tokens=None, reasoning_effort=None):
        self.calls.append(list(messages))
        script = self.scripts.pop(0)

        async def iterate():
            for chunk in script:
                yield chunk

        return iterate()

    async def complete(self, messages, *, tools=None, max_tokens=None):
        return LLMResponse("Title")


def test_resumed_session_sees_previous_history(tmp_path: Path) -> None:
    """A second chat on the same session must observe the first turn's history."""
    client = RecordingClient(
        [
            [LLMStreamChunk(content_delta="First reply.", finish_reason="stop")],
            [LLMStreamChunk(content_delta="Second reply.", finish_reason="stop")],
        ]
    )
    raw_store = SQLiteStore(":memory:")
    core = AgentCore(
        store=AgentStoreAdapter(raw_store),
        clients=LLMClientFactory(client),
        skills=SkillManager(tmp_path / "skills"),
        context_config=ContextConfig(token_budget=4_000),
        workspace_root=tmp_path,
    )
    app = create_app(make_config(tmp_path), store=raw_store, agent=core)

    with TestClient(app, headers={"X-Workspace-ID": "default"}) as client_http:
        session = client_http.post("/api/sessions", json={}).json()
        with client_http.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "hello"},
        ) as response:
            first_events = event_payloads(sse_events(response.iter_lines()))
        assert first_events[-1]["finish_reason"] == "complete"

        with client_http.stream(
            "POST",
            "/api/chat",
            json={"session_id": session["id"], "message": "continue"},
        ) as response:
            second_events = event_payloads(sse_events(response.iter_lines()))
        assert second_events[-1]["finish_reason"] == "complete"

        # The second model call saw user + assistant from turn one, then user.
        second_call = client.calls[1]
        roles = [message.role for message in second_call]
        assert roles == ["user", "assistant", "user"]
        assert second_call[0].content == "hello"
        assert second_call[1].content == "First reply."
        assert second_call[2].content == "continue"

        messages = client_http.get(
            f"/api/sessions/{session['id']}/messages"
        ).json()["messages"]
        assert [m["kind"] for m in messages] == ["message", "message", "message", "message"]


class ServerBlockingAgent:
    """Blocks mid-stream until aborted; otherwise completes after a short delay.

    Fully driven over HTTP: the abort endpoint sets the per-session event. A
    non-aborted stream finishes on its own after ``wait_s`` so tests never hang.
    """

    persists_messages = False

    def __init__(self, wait_s: float = 1.0):
        self.wait_s = wait_s
        self._events: dict[str, asyncio.Event] = {}
        self.aborted: set[str] = set()

    def _event(self, session_id: str) -> asyncio.Event:
        return self._events.setdefault(session_id, asyncio.Event())

    async def stream(self, **kwargs):
        session_id = kwargs["session_id"]
        yield {"type": "text_delta", "delta": "first"}
        try:
            await asyncio.wait_for(self._event(session_id).wait(), timeout=self.wait_s)
        except asyncio.TimeoutError:
            pass
        if session_id in self.aborted:
            return
        await asyncio.sleep(0.1)  # let the sibling stream proceed independently
        yield {"type": "text_delta", "delta": "second"}
        yield {"type": "done", "finish_reason": "stop"}

    async def abort(self, session_id: str, request_id: str | None = None):
        self.aborted.add(session_id)
        self._event(session_id).set()
        return True


def test_abort_mid_stream_ends_with_single_done(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), agent=ServerBlockingAgent())
    with running_server(app) as base_url:
        with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
            session = client.post(
                "/api/sessions", json={}, headers={"X-Workspace-ID": "default"}
            ).json()

            with client.stream(
                "POST",
                "/api/chat",
                json={"session_id": session["id"], "message": "hi"},
                headers={"X-Workspace-ID": "default"},
            ) as response:
                assert response.status_code == 200
                lines = response.iter_lines()
                first_frame = next_sse_frame(lines)
                assert "first" in " ".join(first_frame)

                aborted = client.post(
                    "/api/chat/abort",
                    json={"session_id": session["id"]},
                    headers={"X-Workspace-ID": "default"},
                )
                assert aborted.status_code == 200
                assert aborted.json()["aborted"] is True

                rest = list(lines)  # ends as soon as the stream aborts

            payloads = event_payloads(sse_events([*first_frame, "", *rest]))
            assert [p["type"] for p in payloads] == ["text_delta", "done"]
            assert payloads[-1]["finish_reason"] == "aborted"
            assert payloads[-1]["session_id"] == session["id"]
            assert sum(p["type"] == "done" for p in payloads) == 1


def test_second_chat_on_active_session_is_rejected_with_409(tmp_path: Path) -> None:
    app = create_app(make_config(tmp_path), agent=ServerBlockingAgent())
    with running_server(app) as base_url:
        with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client:
            headers = {"X-Workspace-ID": "default"}
            session = client.post("/api/sessions", json={}, headers=headers).json()

            with client.stream(
                "POST",
                "/api/chat",
                json={"session_id": session["id"], "message": "first"},
                headers=headers,
            ) as response:
                assert response.status_code == 200
                # Keep a reference to the line iterator: dropping it lets the
                # generator be garbage-collected, which closes the response
                # stream and makes the server see a client disconnect (the run
                # is then finished and the 409 below would not apply).
                lines = response.iter_lines()
                next_sse_frame(lines)  # wait until the run is open
                conflict = client.post(
                    "/api/chat",
                    json={"session_id": session["id"], "message": "second"},
                    headers=headers,
                )
                assert conflict.status_code == 409
                assert "already active" in conflict.json()["detail"]

                # End the first run, then verify a fresh chat is accepted.
                client.post(
                    "/api/chat/abort",
                    json={"session_id": session["id"]},
                    headers=headers,
                )
                list(lines)

            again = client.post(
                "/api/chat",
                json={"session_id": session["id"], "message": "third"},
                headers=headers,
            )
            assert again.status_code == 200


def test_aborting_one_user_does_not_affect_another(tmp_path: Path) -> None:
    """Multi-user isolation under abort: A's abort never touches B's stream."""
    app = create_app(make_config(tmp_path), agent=ServerBlockingAgent())
    with running_server(app) as base_url:
        with httpx.Client(base_url=base_url, timeout=15, trust_env=False) as client_a, httpx.Client(
            base_url=base_url, timeout=15, trust_env=False
        ) as client_b:
            headers_a = {"X-Workspace-ID": "ws-a"}
            headers_b = {"X-Workspace-ID": "ws-b"}
            session_a = client_a.post("/api/sessions", json={}, headers=headers_a).json()
            session_b = client_b.post("/api/sessions", json={}, headers=headers_b).json()

            with client_a.stream(
                "POST",
                "/api/chat",
                json={"session_id": session_a["id"], "message": "hi"},
                headers=headers_a,
            ) as resp_a, client_b.stream(
                "POST",
                "/api/chat",
                json={"session_id": session_b["id"], "message": "hi"},
                headers=headers_b,
            ) as resp_b:
                assert resp_a.status_code == 200 and resp_b.status_code == 200
                lines_a = resp_a.iter_lines()
                lines_b = resp_b.iter_lines()
                frame_a = next_sse_frame(lines_a)
                frame_b = next_sse_frame(lines_b)
                assert "first" in " ".join(frame_a)
                assert "first" in " ".join(frame_b)

                aborted = client_a.post(
                    "/api/chat/abort",
                    json={"session_id": session_a["id"]},
                    headers=headers_a,
                )
                assert aborted.status_code == 200
                assert aborted.json()["aborted"] is True

                rest_a = list(lines_a)  # returns promptly after abort
                rest_b = list(lines_b)  # waits out the delay, then completes

            payloads_a = event_payloads(sse_events([*frame_a, "", *rest_a]))
            payloads_b = event_payloads(sse_events([*frame_b, "", *rest_b]))

            # A ended with a single aborted done and nothing after it.
            assert [p["type"] for p in payloads_a] == ["text_delta", "done"]
            assert payloads_a[-1]["finish_reason"] == "aborted"
            assert sum(p["type"] == "done" for p in payloads_a) == 1

            # B streamed its second delta and completed normally.
            assert [p["type"] for p in payloads_b] == ["text_delta", "text_delta", "done"]
            assert payloads_b[-1]["finish_reason"] == "stop"
            assert "second" in payloads_b[1]["delta"]
