"""FastAPI composition root for browser-local chat state."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.agent.context import ContextConfig
from server.agent.core import AgentConfig
from server.agent.registry import ToolRegistry
from server.agent.skills import SkillManager
from server.agent.tools import (
    calculator_tool,
    export_file_tool,
    file_tools,
    load_skill_tool,
    remove_skill_tool,
)
from server.api import APIServices, api_router
from server.api.middleware import AuthMiddleware
from server.api.services import SkillCatalogAdapter
from server.config import AppConfig, load_config
from server.request_runtime import RequestRuntimeFactory
from server.services.document_exporter import DocumentExporter
from server.workspace import IsolatedWorkspaceResolver


class SPAStaticFiles(StaticFiles):
    """Serve Vite assets and fall back to the SPA for non-API routes."""

    async def get_response(self, path: str, scope: dict) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            normalized = path.replace("\\", "/")
            if (
                exc.status_code == 404
                and "." not in Path(normalized).name
                and not normalized.startswith("api/")
            ):
                return await super().get_response("index.html", scope)
            raise


def create_app(
    config: AppConfig | None = None,
    *,
    runtime_factory: Any | None = None,
    skill_manager: SkillManager | None = None,
    document_exporter: DocumentExporter | None = None,
    workspace_resolver: Any | None = None,
) -> FastAPI:
    """Compose only trusted assets and request factories.

    There is intentionally no SQLite, Redis, Fernet, default provider key,
    user-config repository, or server conversation object in this function.
    """

    config = config or load_config()
    document_exporter = document_exporter or DocumentExporter()
    workspace_resolver = workspace_resolver or IsolatedWorkspaceResolver(config.workspace.root)
    skill_manager = skill_manager or SkillManager(_resolve_skills_directory(config))
    if runtime_factory is None:
        agent_config = AgentConfig(
            max_turns=config.agent.max_turns,
            max_tool_retries=config.agent.max_tool_retries,
            tool_timeout_s=config.agent.tool_timeout_s,
            tool_result_max_chars=config.agent.tool_result_max_chars,
            default_skills=config.agent.default_skills,
        )
        for name in config.agent.default_skills:
            skill_manager.get(name)
        tools = ToolRegistry(
            [
                calculator_tool(),
                *file_tools(),
                export_file_tool(document_exporter),
                load_skill_tool(skill_manager),
                remove_skill_tool(skill_manager, protected_names=config.agent.default_skills),
            ]
        )
        runtime_factory = RequestRuntimeFactory(
            skills=skill_manager,
            tools=tools,
            agent_config=agent_config,
            context_config=ContextConfig(
                token_budget=config.context.token_budget,
                summary_trigger_ratio=config.context.summary_trigger_ratio,
                min_recent_messages=config.context.preserve_recent_messages,
            ),
            workspace_root=str(config.workspace.root),
            workspace_resolver=workspace_resolver,
        )

    services = APIServices(
        config=config,
        skill_catalog=SkillCatalogAdapter(skill_manager),
        runtime_factory=runtime_factory,
        document_exporter=document_exporter,
        workspace_resolver=workspace_resolver,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        skill_manager.scan()
        yield

    app = FastAPI(title="Agent Lake", version="0.2.0", lifespan=lifespan)
    app.state.services = services
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(config.server.cors_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "X-Workspace-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.include_router(api_router)

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(_: Request, __: RequestValidationError) -> JSONResponse:
        # FastAPI's default validation payload includes the rejected ``input``;
        # that can be a provider key or a complete conversation message.
        return JSONResponse(status_code=422, content={"detail": "request_invalid"})

    @app.exception_handler(Exception)
    async def private_exception_handler(request: Request, _: Exception) -> JSONResponse:
        # Keep exceptions (which may contain prompts, tool output, or provider
        # response bodies) out of both the HTTP body and app logging.
        if request.url.path.startswith("/api/"):
            return JSONResponse(status_code=500, content={"detail": "internal_error"})
        return JSONResponse(status_code=500, content={"detail": "internal_error"})

    @app.get("/api/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    static_dir = config.server.static_dir
    if static_dir.is_dir() and (static_dir / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="spa")
    else:

        @app.get("/", include_in_schema=False)
        async def api_root() -> JSONResponse:
            return JSONResponse({"name": "Agent Lake API", "status": "ok"})

    return app


def _resolve_skills_directory(config: AppConfig) -> Path:
    candidates: list[Path] = []
    if config.workspace.skills_root is not None:
        candidates.append(config.workspace.skills_root)
    if config.config_path is not None:
        candidates.append(config.config_path.parent / "skills")
    candidates.append(Path(__file__).resolve().parents[1] / "skills")
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return candidates[0]


def get_app() -> FastAPI:
    global app
    existing = globals().get("app")
    if existing is None:
        app = create_app()
    return app


def __getattr__(name: str) -> Any:
    if name == "app":
        return get_app()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    app = get_app()
    uvicorn.run("server.main:app", host=app.state.services.config.server.host, port=app.state.services.config.server.port)


__all__ = ["app", "create_app", "get_app"]
