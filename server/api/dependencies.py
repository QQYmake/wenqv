"""FastAPI dependency helpers."""

from __future__ import annotations

import re

from fastapi import Header, HTTPException, Request

from .services import APIServices


_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def get_services(request: Request) -> APIServices:
    return request.app.state.services


def get_workspace_id(
    request: Request, x_workspace_id: str | None = Header(default=None)
) -> str:
    services = get_services(request)
    workspace_id = x_workspace_id or services.config.workspace.default_id
    if not _WORKSPACE_ID.fullmatch(workspace_id):
        raise HTTPException(status_code=400, detail="Invalid X-Workspace-ID")
    return workspace_id


__all__ = ["get_services", "get_workspace_id"]
