"""Request-local streaming chat and abort endpoints."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any
import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from server.request_runtime import RequestRuntime

from .dependencies import get_services, get_workspace_id
from .schemas import AbortChatRequest, ChatRequest
from .services import APIServices, ActiveRun, AgentAdapter


router = APIRouter(prefix="/chat", tags=["chat"])


def encode_sse(event: Mapping[str, Any]) -> bytes:
    event_type = str(event.get("type", "message"))
    payload = json.dumps(dict(event), ensure_ascii=False, separators=(",", ":"), default=str)
    return f"event: {event_type}\ndata: {payload}\n\n".encode("utf-8")


@router.post("")
async def chat(
    body: ChatRequest,
    request: Request,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> StreamingResponse:
    known_skills = {skill["name"] for skill in services.skill_catalog.list_public()}
    if any(skill not in known_skills for skill in body.skills):
        raise HTTPException(status_code=422, detail="skill_invalid")
    run = await services.runs.start(workspace_id, body.session_id, body.request_id)
    if run is None:
        raise HTTPException(status_code=409, detail="chat_run_active")
    try:
        runtime = await services.runtime_factory.create(
            session_id=body.session_id,
            runtime_context=body.runtime_context.model_dump(mode="json"),
            provider_config=body.provider_config.model_dump(mode="json"),
        )
    except ValueError as exc:
        await services.runs.finish(workspace_id, body.session_id, run.request_id)
        code = str(exc)
        if code not in {"provider_config_invalid", "runtime_context_invalid", "skill_invalid"}:
            code = "chat_request_invalid"
        raise HTTPException(status_code=422, detail=code) from None
    except Exception:
        await services.runs.finish(workspace_id, body.session_id, run.request_id)
        raise HTTPException(status_code=503, detail="chat_unavailable") from None

    agent = AgentAdapter(runtime.agent)
    await services.runs.bind_abort_handler(
        workspace_id,
        body.session_id,
        run.request_id,
        lambda: agent.abort(body.session_id, run.request_id),
    )
    return StreamingResponse(
        _chat_stream(
            body=body,
            request=request,
            workspace_id=workspace_id,
            run=run,
            services=services,
            runtime=runtime,
            agent=agent,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
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
    run: ActiveRun,
    services: APIServices,
    runtime: RequestRuntime,
    agent: AgentAdapter,
) -> AsyncIterator[bytes]:
    terminal: dict[str, Any] | None = None
    try:
        async for event in agent.stream(
            session_id=body.session_id,
            message=body.message,
            selected_skills=body.skills,
            workspace_id=workspace_id,
            request_id=run.request_id,
            reasoning_effort=body.reasoning_effort,
        ):
            if await request.is_disconnected():
                run.abort_event.set()
                await agent.abort(body.session_id, run.request_id)
                return
            if run.abort_event.is_set() and event["type"] != "done":
                terminal = _done(body.session_id, run.request_id, "aborted")
                break
            if event["type"] == "done":
                terminal = dict(event)
                break
            yield encode_sse(event)

        if terminal is None:
            terminal = _done(
                body.session_id,
                run.request_id,
                "aborted" if run.abort_event.is_set() else "stop",
            )
        terminal.setdefault("session_id", body.session_id)
        terminal.setdefault("request_id", run.request_id)
        if run.abort_event.is_set():
            terminal["finish_reason"] = "aborted"

        # Send the canonical, possibly summarized context before ``done`` so a
        # browser can atomically persist it while retaining its full timeline.
        yield encode_sse(
            {
                "type": "conversation_state",
                "session_id": body.session_id,
                "request_id": run.request_id,
                "runtime_context": await runtime.snapshot(),
            }
        )
        yield encode_sse(terminal)
    except asyncio.CancelledError:
        run.abort_event.set()
        await agent.abort(body.session_id, run.request_id)
        raise
    except Exception:
        # Provider exceptions can include credentials, prompts, or remote body
        # text.  The response deliberately contains only a stable code.
        yield encode_sse({"type": "error", "code": "chat_failed", "message": "chat_failed"})
        try:
            snapshot = await runtime.snapshot()
        except Exception:
            snapshot = {"messages": [], "active_skills": []}
        yield encode_sse(
            {
                "type": "conversation_state",
                "session_id": body.session_id,
                "request_id": run.request_id,
                "runtime_context": snapshot,
            }
        )
        yield encode_sse(_done(body.session_id, run.request_id, "error"))
    finally:
        await runtime.close()
        await services.runs.finish(workspace_id, body.session_id, run.request_id)


def _done(session_id: str, request_id: str, finish_reason: str) -> dict[str, str]:
    return {
        "type": "done",
        "session_id": session_id,
        "request_id": request_id,
        "finish_reason": finish_reason,
    }


@router.post("/abort")
async def abort_chat(
    body: AbortChatRequest,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict[str, Any]:
    aborted = await services.runs.request_abort(
        workspace_id, body.session_id, body.request_id
    )
    return {"aborted": aborted, "session_id": body.session_id}


__all__ = ["encode_sse", "router"]
