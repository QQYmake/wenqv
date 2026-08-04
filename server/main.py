"""FastAPI composition root for the Agent chat application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from inspect import Parameter, signature
from pathlib import Path
from typing import Any
import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.api import APIServices, AgentAdapter, api_router
from server.api.middleware import AuthMiddleware
from server.api.services import SkillCatalogAdapter, UnavailableAgent
from server.config import AppConfig, LLMProviderConfig, load_config
from server.storage import AgentStoreAdapter, SQLiteStore, build_side_cache


logger = logging.getLogger(__name__)


class SPAStaticFiles(StaticFiles):
    """Serve Vite assets and fall back to index.html for client routes."""

    async def get_response(self, path: str, scope: dict) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code == 404 and "." not in Path(path).name:
                return await super().get_response("index.html", scope)
            raise


def create_app(
    config: AppConfig | None = None,
    *,
    store: SQLiteStore | None = None,
    agent: Any | None = None,
    skill_manager: Any | None = None,
    title_generator: Any | None = None,
) -> FastAPI:
    """Create an app with injectable adapters for integration tests."""

    config = config or load_config()
    if store is None:
        cache = build_side_cache(
            config.storage.redis_url, ttl_s=config.storage.cache_ttl_s
        )
        store = SQLiteStore(config.storage.sqlite_path, cache=cache)
    agent_store = AgentStoreAdapter(store)

    if agent is None:
        agent, default_skills, default_title_generator = _compose_agent(
            config, agent_store
        )
        skill_manager = skill_manager or default_skills
        title_generator = title_generator or default_title_generator

    services = APIServices(
        config=config,
        store=store,
        agent=AgentAdapter(agent or UnavailableAgent()),
        skill_catalog=SkillCatalogAdapter(skill_manager),
        agent_store=agent_store,
        title_generator=title_generator,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await store.initialize()
        await store.ensure_workspace(
            config.workspace.default_id, config.workspace.default_name
        )
        if skill_manager is not None and hasattr(skill_manager, "scan"):
            skill_manager.scan()
        try:
            yield
        finally:
            close_agent = getattr(agent, "close", None)
            if close_agent is not None:
                result = close_agent()
                if hasattr(result, "__await__"):
                    await result
            await store.close()

    origins = list(config.server.cors_origins)
    if any(origin == "*" for origin in origins):
        raise ValueError(
            "CORS allow_credentials=True forbids the wildcard origin; "
            "configure explicit origins in config.server.cors_origins."
        )

    app = FastAPI(
        title="Agent Lake",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.services = services
    # Order matters: the last add_middleware call becomes the outermost layer.
    # CORS must run first so preflights are answered and credentials are
    # exposed to listed origins before the Auth guard runs; Auth then injects
    # the workspace header and guards private API paths.
    app.add_middleware(AuthMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept", "X-Workspace-ID"],
        expose_headers=["X-Request-ID"],
    )
    app.include_router(api_router)

    @app.get("/api/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    static_dir = config.server.static_dir
    if static_dir.is_dir() and (static_dir / "index.html").is_file():
        app.mount("/", SPAStaticFiles(directory=static_dir, html=True), name="spa")
    else:
        @app.get("/", include_in_schema=False)
        async def api_root() -> JSONResponse:
            return JSONResponse(
                {"name": "Agent Lake API", "docs": "/docs", "status": "ok"}
            )

    return app


def _compose_agent(
    config: AppConfig, agent_store: AgentStoreAdapter
) -> tuple[Any, Any | None, Any | None]:
    """Lazily compose the framework-free core, keeping imports at the edge."""

    skill_manager: Any | None = None
    context_manager: Any | None = None
    try:
        from server.agent.context import ContextConfig, ContextManager
        from server.agent.core import AgentConfig, AgentCore
        from server.agent.llm import LLMClientFactory
        from server.agent.registry import ToolRegistry
        from server.agent.skills import SkillManager
        from server.agent.tools import (
            calculator_tool,
            load_skill_tool,
            read_file_tool,
            remove_skill_tool,
        )

        skills_dir = config.workspace.root / "skills"
        skill_manager = SkillManager(skills_dir)
        clients = LLMClientFactory(
            _llm_mapping(config.llm.main),
            (
                _llm_mapping(config.llm.summary)
                if config.llm.summary is not None
                else None
            ),
        )
        context_manager = ContextManager(
            clients,
            ContextConfig(
                token_budget=config.context.token_budget,
                summary_trigger_ratio=config.context.summary_trigger_ratio,
                min_recent_messages=config.context.preserve_recent_messages,
            ),
        )
        registry = ToolRegistry(
            [
                calculator_tool(),
                read_file_tool(),
                load_skill_tool(skill_manager),
                remove_skill_tool(skill_manager),
            ]
        )
        agent_config = _construct_supported(
            AgentConfig,
            {
                "max_turns": config.agent.max_turns,
                "max_tool_retries": config.agent.max_tool_retries,
                "tool_timeout_s": config.agent.tool_timeout_s,
                "tool_result_max_chars": config.agent.tool_result_max_chars,
                "max_result_chars": config.agent.tool_result_max_chars,
            },
        )
        core = _construct_supported(
            AgentCore,
            {
                "clients": clients,
                "client_provider": clients,
                "llm": clients,
                "store": agent_store,
                "conversation_store": agent_store,
                "tools": registry,
                "registry": registry,
                "tool_registry": registry,
                "skills": skill_manager,
                "skill_manager": skill_manager,
                "context": context_manager,
                "context_manager": context_manager,
                "config": agent_config,
                "workspace_root": config.workspace.root,
                "workspace_resolver": lambda _workspace_id: config.workspace.root,
            },
        )
        return core, skill_manager, context_manager
    except Exception as exc:
        logger.exception("Agent Core composition failed")
        return UnavailableAgent(str(exc)), skill_manager, context_manager


def _construct_supported(factory: Any, candidates: dict[str, Any]) -> Any:
    parameters = signature(factory).parameters
    accepts_extra = any(
        parameter.kind == Parameter.VAR_KEYWORD for parameter in parameters.values()
    )
    values = (
        candidates
        if accepts_extra
        else {name: value for name, value in candidates.items() if name in parameters}
    )
    return factory(**values)


def _llm_mapping(provider: LLMProviderConfig) -> dict[str, Any]:
    return asdict(provider)


app = create_app()


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    uvicorn.run("server.main:app", host=app.state.services.config.server.host, port=app.state.services.config.server.port)


__all__ = ["app", "create_app"]
