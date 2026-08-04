"""Workspace-scoped session and message endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status

from .dependencies import get_services, get_workspace_id
from .schemas import CreateSessionRequest, RenameSessionRequest
from .services import APIServices


router = APIRouter(prefix="/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    sessions = await services.store.list_sessions(workspace_id, limit=limit, offset=offset)
    return {"sessions": sessions}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_session(
    body: CreateSessionRequest,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    return await services.store.create_session(workspace_id, body.title)


@router.patch("/{session_id}")
async def rename_session(
    session_id: str,
    body: RenameSessionRequest,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    session = await services.store.rename_session(session_id, body.title, workspace_id)
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return session


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: str,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> Response:
    if not await services.store.delete_session(session_id, workspace_id):
        raise HTTPException(status_code=404, detail="Session not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{session_id}/messages")
async def list_messages(
    session_id: str,
    limit: int | None = Query(default=None, ge=1, le=5_000),
    before_sequence: int | None = Query(default=None, ge=1),
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    messages = await services.store.list_messages(
        session_id,
        workspace_id,
        limit=limit,
        before_sequence=before_sequence,
    )
    if messages is None:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"messages": messages}


__all__ = ["router"]
