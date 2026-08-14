"""FastAPI composition root for the Agent chat application."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from inspect import Parameter, signature
from pathlib import Path
from typing import Any
import logging

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from server.api import APIServices, AgentAdapter, api_router
from server.api.middleware import AuthMiddleware
from server.api.services import SkillCatalogAdapter, UnavailableAgent
from server.config import AppConfig, LLMProviderConfig, load_config
from server.storage import (
    AgentStoreAdapter,
    IsolatedWorkspaceResolver,
    SQLiteStore,
    build_side_cache,
)
from server.storage.encryption import EncryptionError
from server.services.document_exporter import DocumentExporter


logger = logging.getLogger(__name__)


class SPAStaticFiles(StaticFiles):
    """Serve Vite assets and fall back to index.html for client routes.

    API paths (``/api/*``) are excluded from the fallback: a missing API route
    must surface as a JSON 404, never as the SPA shell (which the frontend then
    fails to parse as JSON, e.g. "Unexpected token '<', \"<!doctype ...").
    """

    async def get_response(self, path: str, scope: dict) -> Any:
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            # StaticFiles normalizes paths with os.sep; on Windows that yields
            # backslashes ("api\user\config"), so compare on a "/" view.
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
    store: SQLiteStore | None = None,
    agent: Any | None = None,
    skill_manager: Any | None = None,
    title_generator: Any | None = None,
    client_resolver: Any | None = None,
    user_config_repo: Any | None = None,
    cipher: Any | None = None,
    document_exporter: DocumentExporter | None = None,
    workspace_resolver: Any | None = None,
) -> FastAPI:
    """Create an app with injectable adapters for integration tests."""

    config = config or load_config()
    if store is None:
        cache = build_side_cache(
            config.storage.redis_url, ttl_s=config.storage.cache_ttl_s
        )
        store = SQLiteStore(config.storage.sqlite_path, cache=cache)
    agent_store = AgentStoreAdapter(store)
    document_exporter = document_exporter or DocumentExporter()
    workspace_resolver = workspace_resolver or IsolatedWorkspaceResolver(config.workspace.root)

    # Per-user LLM configuration: Fernet cipher for the api_key, a repository
    # for encrypted read/write, and a ClientResolver adapter that merges user
    # config with the default llm.* and pools clients. Tests inject fakes.
    if cipher is None:
        cipher = _build_cipher(config)
    if user_config_repo is None:
        user_config_repo = _build_user_config_repo(store, cipher)
    if client_resolver is None:
        client_resolver = _build_client_resolver(user_config_repo, config.llm)

    if agent is None:
        agent, default_skills, default_title_generator = _compose_agent(
            config,
            agent_store,
            store,
            client_resolver,
            document_exporter,
            workspace_resolver,
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
        client_resolver=client_resolver,
        user_config_repo=user_config_repo,
        document_exporter=document_exporter,
        workspace_resolver=workspace_resolver,
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

    @app.exception_handler(EncryptionError)
    async def _encryption_error_handler(request: Request, exc: EncryptionError) -> JSONResponse:
        """A wrong/missing AGENT_SECRET_KEY must fail with a clear, key-free error.

        The stored ciphertext is unreadable (e.g. the key changed between
        restarts). We never echo the exception or any key material to the client.
        """

        logger.warning("User config decryption failed; AGENT_SECRET_KEY may have changed")
        return JSONResponse(
            status_code=500,
            content={"detail": "用户配置解密失败：请检查服务端 AGENT_SECRET_KEY"},
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
    config: AppConfig,
    agent_store: AgentStoreAdapter,
    store: SQLiteStore,
    client_resolver: Any,
    document_exporter: DocumentExporter | None = None,
    workspace_resolver: Any | None = None,
) -> tuple[Any, Any | None, Any | None]:
    """Lazily compose the framework-free core, keeping imports at the edge."""

    skill_manager: Any | None = None
    context_manager: Any | None = None
    try:
        from server.agent.context import ContextConfig, ContextManager
        from server.agent.core import AgentConfig, AgentCore
        from server.agent.registry import ToolRegistry
        from server.agent.skills import SkillManager
        from server.agent.tools import (
            calculator_tool,
            export_file_tool,
            file_tools,
            load_skill_tool,
            remove_skill_tool,
        )

        workspace_resolver = workspace_resolver or IsolatedWorkspaceResolver(
            config.workspace.root
        )
        skills_dir = config.workspace.root / "skills"
        skill_manager = SkillManager(skills_dir)
        agent_config = _construct_supported(
            AgentConfig,
            {
                "max_turns": config.agent.max_turns,
                "max_tool_retries": config.agent.max_tool_retries,
                "tool_timeout_s": config.agent.tool_timeout_s,
                "tool_result_max_chars": config.agent.tool_result_max_chars,
                "max_result_chars": config.agent.tool_result_max_chars,
                "default_skills": config.agent.default_skills,
            },
        )
        for name in config.agent.default_skills:
            skill_manager.get(name)
        # The ClientResolver port replaces the startup-time LLMClientFactory
        # singleton. Per-workspace user configs are honoured via the resolver;
        # when no user config exists it falls back to config.llm.* defaults.
        clients = client_resolver
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
                *file_tools(),
                export_file_tool(document_exporter),
                load_skill_tool(skill_manager),
                remove_skill_tool(
                    skill_manager,
                    protected_names=config.agent.default_skills,
                ),
            ]
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
                "workspace_resolver": workspace_resolver,
                "document_exporter": document_exporter,
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


def _build_cipher(config: AppConfig) -> Any:
    """Build the Fernet cipher from AGENT_SECRET_KEY.

    When ``llm.require_user_config`` is true the per-user config path is the
    primary flow: a missing AGENT_SECRET_KEY fails fast at startup so encrypted
    keys are never silently unreadable. When it is false (the legacy default
    path), an ephemeral dev key is used and a warning is logged.
    """

    from server.storage.encryption import EncryptionError, FernetCipher, KeyManager

    key_manager = KeyManager()
    if not key_manager.configured:
        if config.llm.require_user_config:
            raise EncryptionError(
                "AGENT_SECRET_KEY is not set but llm.require_user_config is true; "
                "set AGENT_SECRET_KEY before starting the server."
            )
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("cryptography is required for user API key storage") from exc
        key_manager = KeyManager(Fernet.generate_key())
        logger.warning(
            "AGENT_SECRET_KEY is not set; using an ephemeral key. "
            "User API keys will not survive a restart. Set AGENT_SECRET_KEY in production."
        )
    else:
        # Fail fast when the key is present but malformed: an invalid Fernet key
        # would otherwise 500 on the first encrypt/decrypt (e.g. a manually set
        # AGENT_SECRET_KEY that is not 32 urlsafe base64 bytes).
        try:
            from cryptography.fernet import Fernet

            Fernet(key_manager.require())
        except Exception as exc:  # pragma: no cover - environment misconfiguration
            raise EncryptionError(
                "AGENT_SECRET_KEY is not a valid Fernet key (must be 32 url-safe "
                "base64 bytes). Delete .agent_secret_key (or set a fresh key) and "
                "restart via start.bat to generate a valid one."
            ) from exc
    return FernetCipher(key_manager)


def _build_user_config_repo(store: SQLiteStore, cipher: Any) -> Any:
    from server.storage.user_configs import UserConfigRepository

    return UserConfigRepository(store, cipher)


def _build_client_resolver(user_config_repo: Any, default_llm: Any) -> Any:
    from server.llm_resolver import LLMResolverAdapter

    return LLMResolverAdapter(user_config_repo, default_llm)


def get_app() -> FastAPI:
    """Create the production app lazily.

    ``app`` is exposed as a module-level attribute via ``__getattr__`` so that
    simply importing server.main (e.g. in tests) does not eagerly build it —
    important because the production config.yaml sets
    ``llm.require_user_config=true``, which fails fast when AGENT_SECRET_KEY is
    unset. Note there must be NO module-level ``app = None`` binding: it would
    shadow ``__getattr__`` and make ``uvicorn server.main:app`` resolve to
    ``None``, crashing every request with 500.
    """

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

    uvicorn.run(
        "server.main:app",
        host=get_app().state.services.config.server.host,
        port=get_app().state.services.config.server.port,
    )


__all__ = ["app", "create_app", "get_app"]
