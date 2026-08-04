"""Streaming chat and abort endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any
import asyncio
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from .dependencies import get_services, get_workspace_id
from .schemas import AbortChatRequest, ChatRequest
from .services import APIServices, ActiveRun


router = APIRouter(prefix="/chat", tags=["chat"])


def encode_sse(event: Mapping[str, Any]) -> bytes:
    """Encode one named SSE frame with a JSON payload."""

    event_type = str(event.get("type", "message"))
    payload = json.dumps(
        dict(event), ensure_ascii=False, separators=(",", ":"), default=str
    )
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


@router.post("")
async def chat(
    body: ChatRequest,
    request: Request,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> StreamingResponse:
    session = await services.store.get_session(body.session_id, workspace_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    run = await services.runs.start(workspace_id, body.session_id, body.request_id)
    if run is None:
        raise HTTPException(status_code=409, detail="A chat run is already active")

    stream = _chat_stream(
        body=body,
        request=request,
        workspace_id=workspace_id,
        initial_session=session,
        run=run,
        services=services,
    )
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
            "X-Request-ID": run.request_id,
        },
    )


async def _chat_stream(
    *,
    body: ChatRequest,
    request: Request,
    workspace_id: str,
    initial_session: Mapping[str, Any],
    run: ActiveRun,
    services: APIServices,
) -> AsyncIterator[bytes]:
    terminal: dict[str, Any] | None = None
    assistant_parts: list[str] = []
    transport_persists = not services.agent.persists_messages
    try:
        if transport_persists:
            await services.store.add_message(body.session_id, "user", body.message)

        async for event in services.agent.stream(
            session_id=body.session_id,
            message=body.message,
            selected_skills=body.skills,
            workspace_id=workspace_id,
            request_id=run.request_id,
        ):
            if await request.is_disconnected():
                run.abort_event.set()
                await services.agent.abort(body.session_id, run.request_id)
                return
            if run.abort_event.is_set() and event["type"] != "done":
                terminal = {
                    "type": "done",
                    "session_id": body.session_id,
                    "request_id": run.request_id,
                    "finish_reason": "aborted",
                }
                break
            if event["type"] == "done":
                terminal = dict(event)
                break
            if event["type"] == "text_delta":
                assistant_parts.append(str(event.get("delta", "")))
            if transport_persists:
                await _persist_transport_event(services, body.session_id, event)
            yield encode_sse(event)

        if transport_persists and assistant_parts:
            await services.store.add_message(
                body.session_id, "assistant", "".join(assistant_parts)
            )
        if terminal is None:
            terminal = {
                "type": "done",
                "session_id": body.session_id,
                "request_id": run.request_id,
                "finish_reason": "aborted" if run.abort_event.is_set() else "stop",
            }
        terminal.setdefault("session_id", body.session_id)
        terminal.setdefault("request_id", run.request_id)
        if run.abort_event.is_set():
            terminal["finish_reason"] = "aborted"

        yield encode_sse(terminal)
        # The client receives its terminal event immediately. Title generation
        # remains best-effort and tightly bounded so a slow summary provider
        # cannot hold the stream open for its full model timeout.
        await _generate_initial_title(
            services, initial_session, body.session_id, body.message
        )
    except asyncio.CancelledError:
        run.abort_event.set()
        await services.agent.abort(body.session_id, run.request_id)
        raise
    except Exception as exc:
        yield encode_sse(
            {
                "type": "error",
                "code": "chat_failed",
                "message": str(exc) or type(exc).__name__,
                "recoverable": False,
            }
        )
        yield encode_sse(
            {
                "type": "done",
                "session_id": body.session_id,
                "request_id": run.request_id,
                "finish_reason": "error",
            }
        )
    finally:
        await services.runs.finish(workspace_id, body.session_id, run.request_id)


async def _persist_transport_event(
    services: APIServices, session_id: str, event: Mapping[str, Any]
) -> None:
    if event["type"] in {"tool_call", "tool_result"}:
        await services.store.add_tool_event(session_id, str(event["type"]), event)


async def _generate_initial_title(
    services: APIServices,
    initial_session: Mapping[str, Any],
    session_id: str,
    first_message: str,
) -> None:
    if int(initial_session.get("message_count", 0)) > 0:
        return
    if str(initial_session.get("title", "")).strip().lower() not in {
        "",
        "new conversation",
        "new chat",
        "新对话",
    }:
        return
    title: str | None = None
    if services.title_generator is not None:
        try:
            messages = await services.agent_store.list_messages(session_id)
            method = getattr(
                services.title_generator, "generate_title", services.title_generator
            )
            result = method(messages)
            if hasattr(result, "__await__"):
                result = await asyncio.wait_for(result, timeout=2.0)
            if hasattr(result, "content"):
                result = result.content
            if result is not None:
                title = _clean_title(str(result))
        except Exception:
            # A summary-model outage must not affect the main chat response.
            title = None
    title = title or _fallback_title(first_message)
    try:
        await services.store.rename_session(session_id, title)
    except Exception:
        pass


def _clean_title(value: str) -> str | None:
    value = value.strip().strip("`#*\"'“”‘’")
    value = re.sub(r"^(title|标题)\s*[:：]\s*", "", value, flags=re.IGNORECASE)
    value = " ".join(value.split())
    return value[:80].rstrip() or None


def _fallback_title(message: str) -> str:
    value = re.sub(r"(?<!\w)@[A-Za-z0-9][A-Za-z0-9_-]{0,63}\b", "", message)
    value = re.sub(r"\s+", " ", value).strip()
    return (value[:48].rstrip(" ,.;，。；") or "New conversation")


@router.post("/abort")
async def abort_chat(
    body: AbortChatRequest,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, Any]:
    session = await services.store.get_session(body.session_id, workspace_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    local = await services.runs.request_abort(
        workspace_id, body.session_id, body.request_id
    )
    core = await services.agent.abort(body.session_id, body.request_id)
    return {"aborted": local or core, "session_id": body.session_id}


__all__ = ["encode_sse", "router"]
