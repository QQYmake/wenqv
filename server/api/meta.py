"""Public skill catalogue and non-secret runtime limits."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from .dependencies import get_services, get_workspace_id
from .services import APIServices


router = APIRouter(tags=["metadata"])


@router.get("/skills")
async def list_skills(
    _: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    return {"skills": services.skill_catalog.list_public()}


@router.get("/config")
async def public_config(
    _: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> dict:
    return services.config.public_dict()


__all__ = ["router"]
