"""Opaque, workspace-scoped document download endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from .dependencies import get_services, get_workspace_id
from .services import APIServices


router = APIRouter(prefix="/files", tags=["files"])


@router.get("/{file_id}")
async def download_file(
    file_id: str,
    workspace_id: str = Depends(get_workspace_id),
    services: APIServices = Depends(get_services),
) -> FileResponse:
    exporter = services.document_exporter
    resolver = services.workspace_resolver
    if exporter is None or resolver is None:
        raise HTTPException(status_code=404, detail="File not found")
    try:
        workspace_root = resolver(workspace_id)
    except (OSError, ValueError):
        raise HTTPException(status_code=404, detail="File not found") from None
    exported = exporter.resolve(file_id, workspace_root)
    if exported is None:
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        path=exported.path,
        media_type=exported.mime_type,
        filename=exported.filename,
        headers={"Cache-Control": "no-store"},
    )


__all__ = ["router"]
