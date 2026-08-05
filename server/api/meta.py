"""Skill catalogue and public runtime configuration."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .dependencies import get_services, get_workspace_id
from .middleware.auth import WORKSPACE_COOKIE_NAME, _WORKSPACE_ID
from .services import APIServices


router = APIRouter(tags=["metadata"])


@router.get("/skills")
async def list_skills(
    session_id: str | None = Query(default=None, max_length=128),
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    skills = services.skill_catalog.list_public()
    if session_id is not None:
        session = await services.store.get_session(session_id, workspace_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        loaded_rows = await services.store.list_loaded_skills(session_id, workspace_id)
        loaded = {row["skill_name"] for row in loaded_rows or ()}
        skills = [{**skill, "loaded": skill["name"] in loaded} for skill in skills]
    return {"skills": skills}


@router.get("/config")
async def public_config(services: APIServices = Depends(get_services)) -> dict:
    return services.config.public_dict()


@router.get("/bootstrap")
async def bootstrap(
    request: Request,
    services: APIServices = Depends(get_services),
) -> dict[str, str]:
    """Issue (or refresh) a per-visitor workspace identity cookie.

    The identity is stable across page reloads and tabs: when the request
    already carries a valid ``workspace_id`` cookie (or an explicit
    ``X-Workspace-ID`` header), the same id is returned instead of minting a
    new one. Without reuse, every page load would create a fresh workspace and
    a config saved moments earlier would no longer be visible to the chat flow.

    The cookie is HttpOnly (JS cannot read it), Secure (HTTPS only in prod),
    and SameSite=Lax. The frontend keeps a non-sensitive localStorage mirror so
    the UI can show the active workspace; the cookie is the source of truth
    for requests. Clearing browser data loses the identity.

    The cookie is stable across refreshes: a request that already carries a
    valid workspace_id cookie reuses it instead of rotating the identity,
    so a page reload keeps the same sessions and files. Only a missing or
    malformed cookie gets a fresh identity.
    """
    existing = request.cookies.get(WORKSPACE_COOKIE_NAME)
    if not (existing and _WORKSPACE_ID.fullmatch(existing)):
        header = request.headers.get("X-Workspace-ID")
        existing = header if header and _WORKSPACE_ID.fullmatch(header) else None
    workspace_id = existing or str(uuid.uuid4())
    # Idempotent: registers the reused id (e.g. from a preseeded cookie or an
    # explicit header) and keeps the workspaces table consistent with the
    # issued identity.
    await services.store.ensure_workspace(workspace_id)
    response = JSONResponse({"workspace_id": workspace_id})
    response.set_cookie(
        key=WORKSPACE_COOKIE_NAME,
        value=workspace_id,
        httponly=True,
        secure=services.config.server.cookie_secure,
        samesite="lax",
        path="/",
    )
    return response


__all__ = ["router"]
