"""Cookie-based workspace identity middleware.

Extracts the per-user ``workspace_id`` from the ``workspace_id`` cookie (set by
``GET /api/bootstrap``) and injects it as the ``X-Workspace-ID`` header so the
existing workspace-scoped dependencies keep working without any storage-layer
change. The explicit ``X-Workspace-ID`` header remains a valid isolation
primitive (reused by the architecture and by non-browser clients); the cookie
takes precedence when both are present.

Requests without a valid ``workspace_id`` are rejected with ``401`` for API
paths that are not on the public whitelist. Static assets, the SPA fallback,
``GET``/``HEAD /api/bootstrap``, ``GET``/``HEAD /api/health``, ``OPTIONS``
preflights, and the connection-test endpoint are always reachable so a
brand-new visitor can load the page and configure its API before any identity
exists.
"""

from __future__ import annotations

import re
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse


WORKSPACE_COOKIE_NAME = "workspace_id"

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

# API paths reachable before any workspace identity exists.
_PUBLIC_API_GET = {"/api/bootstrap", "/api/health"}
_PUBLIC_API_POST = {"/api/user/config/test"}


def _is_api_path(path: str) -> bool:
    return path == "/api" or path.startswith("/api/")


def _extract_workspace_id(request: Request) -> str | None:
    cookie = request.cookies.get(WORKSPACE_COOKIE_NAME)
    if cookie and _WORKSPACE_ID.fullmatch(cookie):
        return cookie
    header = request.headers.get("X-Workspace-ID")
    if header and _WORKSPACE_ID.fullmatch(header):
        return header
    return None


def _is_whitelisted(request: Request) -> bool:
    if request.method == "OPTIONS":
        return True
    path = request.url.path
    if not _is_api_path(path):
        return True
    # HEAD is the body-less twin of GET (FastAPI routes HEAD onto GET
    # handlers), so health probes must be whitelisted exactly like GET.
    if request.method in ("GET", "HEAD") and path in _PUBLIC_API_GET:
        return True
    if request.method == "POST" and path in _PUBLIC_API_POST:
        return True
    return False


class AuthMiddleware(BaseHTTPMiddleware):
    """Inject ``X-Workspace-ID`` from the cookie and guard private API paths."""

    def __init__(self, app: Any) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Any) -> Any:
        if _is_whitelisted(request):
            return await call_next(request)

        workspace_id = _extract_workspace_id(request)
        if workspace_id is None:
            return JSONResponse(
                {"detail": "Missing workspace identity"}, status_code=401
            )

        # Inject the header for downstream dependencies (get_workspace_id reads
        # the header). Mutating scope headers keeps the request object stable
        # for handlers and middleware that read headers later.
        scope = request.scope
        headers = scope.get("headers") or []
        existing = {name.decode("latin-1").lower(): value for name, value in headers}
        if existing.get("x-workspace-id") != workspace_id.encode("latin-1"):
            headers = [
                (name, value)
                for name, value in headers
                if name.decode("latin-1").lower() != "x-workspace-id"
            ]
            headers.append((b"x-workspace-id", workspace_id.encode("latin-1")))
            scope["headers"] = headers
        return await call_next(request)


__all__ = ["AuthMiddleware", "WORKSPACE_COOKIE_NAME"]