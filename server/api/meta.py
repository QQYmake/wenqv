"""Skill catalogue and public runtime configuration."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from .dependencies import get_services, get_workspace_id
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


__all__ = ["router"]
