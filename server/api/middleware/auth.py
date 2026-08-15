"""Header-only anonymous workspace validation.

The browser mints this opaque id locally and sends it on each private request.
No cookie is issued, read, or mapped to a database row.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_PUBLIC_API_PATHS = {"/api/health"}


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


class AuthMiddleware(BaseHTTPMiddleware):
    """Require a valid ``X-Workspace-ID`` header for private API routes."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        path = request.url.path
        if request.method == "OPTIONS" or not _is_api_path(path) or path in _PUBLIC_API_PATHS:
            response = await call_next(request)
        else:
            workspace_id = request.headers.get("X-Workspace-ID")
            if not workspace_id or not _WORKSPACE_ID.fullmatch(workspace_id):
                return JSONResponse({"detail": "workspace_id_required"}, status_code=401)
            response = await call_next(request)
        # API responses can contain conversations, generated files, and model
        # metadata; none may be retained by browser/proxy HTTP caches.
        if _is_api_path(path):
            response.headers["Cache-Control"] = "no-store"
        return response


__all__ = ["AuthMiddleware"]
