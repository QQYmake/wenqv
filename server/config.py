"""Application configuration with YAML defaults and environment overrides.

The module deliberately uses dataclasses rather than framework settings objects.
That keeps configuration usable by the Agent Core and tests without importing
FastAPI.  Secrets are never included in ``public_dict`` or dataclass reprs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
import os


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _string(value: Any, default: str = "") -> str:
    return default if value is None else str(value)


def _integer(value: Any, default: int, *, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def _float(value: Any, default: float, *, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def _env_bool(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(slots=True)
class LLMProviderConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = field(default="", repr=False)
    model: str = ""
    max_tokens: int | None = None
    timeout_s: float = 120.0
    temperature: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "LLMProviderConfig":
        max_tokens = data.get("max_tokens")
        temperature = data.get("temperature")
        return cls(
            base_url=_string(data.get("base_url"), "https://api.openai.com/v1").rstrip("/"),
            api_key=_string(data.get("api_key")),
            model=_string(data.get("model")),
            max_tokens=(
                _integer(max_tokens, 1) if max_tokens not in (None, "") else None
            ),
            timeout_s=_float(
                data.get("timeout_s"), 120.0, minimum=1.0, maximum=3600.0
            ),
            temperature=(
                _float(temperature, 0.0, minimum=0.0, maximum=2.0)
                if temperature not in (None, "")
                else None
            ),
        )


@dataclass(slots=True)
class LLMSettings:
    main: LLMProviderConfig = field(default_factory=LLMProviderConfig)
    summary: LLMProviderConfig | None = None
    require_user_config: bool = False

    def for_role(self, role: str) -> LLMProviderConfig:
        if role == "main":
            return self.main
        if role == "summary":
            return self.summary or self.main
        raise ValueError(f"Unknown LLM role: {role}")

    @property
    def summary_uses_main(self) -> bool:
        return self.summary is None


@dataclass(slots=True)
class AgentSettings:
    max_turns: int = 20
    max_tool_retries: int = 2
    tool_timeout_s: float = 60.0
    tool_result_max_chars: int = 65_536


@dataclass(slots=True)
class ContextSettings:
    token_budget: int = 32_000
    summary_trigger_ratio: float = 0.8
    preserve_recent_messages: int = 10


@dataclass(slots=True)
class StorageSettings:
    sqlite_path: Path = Path("data/agent.db")
    redis_url: str | None = None
    cache_ttl_s: int = 86_400


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    static_dir: Path = Path("web/dist")
    cookie_secure: bool = True


@dataclass(slots=True)
class WorkspaceSettings:
    default_id: str = "default"
    default_name: str = "Default workspace"
    root: Path = Path(".")

    @property
    def id(self) -> str:
        return self.default_id


@dataclass(slots=True)
class AppConfig:
    llm: LLMSettings = field(default_factory=LLMSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    storage: StorageSettings = field(default_factory=StorageSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    config_path: Path | None = field(default=None, repr=False)

    def get_llm(self, role: str) -> LLMProviderConfig:
        """Return a role configuration, applying the summary -> main fallback."""

        return self.llm.for_role(role)

    def public_dict(self) -> dict[str, Any]:
        """Return the browser-safe subset of configuration."""

        summary = self.llm.for_role("summary")
        return {
            "model_id": self.llm.main.model,
            "summary_model_id": summary.model,
            "summary_uses_main": self.llm.summary_uses_main,
            "limits": {
                "max_turns": self.agent.max_turns,
                "tool_timeout_s": self.agent.tool_timeout_s,
                "tool_result_max_chars": self.agent.tool_result_max_chars,
                "token_budget": self.context.token_budget,
                "preserve_recent_messages": self.context.preserve_recent_messages,
            },
            "features": {
                "streaming": True,
                "skills": True,
                "abort": True,
            },
            "workspace_id": self.workspace.default_id,
        }


def _load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - dependency installation issue
        raise RuntimeError("PyYAML is required to read config.yaml") from exc
    with path.open("r", encoding="utf-8") as stream:
        loaded = yaml.safe_load(stream) or {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Configuration root must be a mapping: {path}")
    return loaded


def _provider_with_env(
    data: Mapping[str, Any], env: Mapping[str, str], prefix: str
) -> LLMProviderConfig:
    merged = dict(data)
    aliases = {
        "API_KEY": "api_key",
        "BASE_URL": "base_url",
        "MODEL": "model",
        "MAX_TOKENS": "max_tokens",
        "TIMEOUT_S": "timeout_s",
        "TEMPERATURE": "temperature",
    }
    for suffix, key in aliases.items():
        env_key = f"{prefix}_{suffix}"
        if env_key in env:
            merged[key] = env[env_key]
    return LLMProviderConfig.from_mapping(merged)


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load ``config.yaml`` and apply explicit ``AGENT_*`` overrides.

    Relative data and SPA paths are resolved from the configuration file's
    directory.  A missing file is valid and produces development defaults.
    """

    env = os.environ if environ is None else environ
    selected_path = Path(path or env.get("AGENT_CONFIG", "config.yaml")).resolve()
    root = selected_path.parent
    raw = _load_yaml(selected_path)

    llm_data = _mapping(raw.get("llm"))
    require_user_config = _env_bool(
        env.get("AGENT_REQUIRE_USER_CONFIG", llm_data.get("require_user_config")),
        False,
    )
    main = _provider_with_env(_mapping(llm_data.get("main")), env, "AGENT_MAIN")
    raw_summary = llm_data.get("summary")
    summary_env_present = any(key.startswith("AGENT_SUMMARY_") for key in env)
    summary = None
    if isinstance(raw_summary, Mapping) or summary_env_present:
        inherited_summary: dict[str, Any] = {
            "base_url": main.base_url,
            "api_key": main.api_key,
            "model": main.model,
            "max_tokens": main.max_tokens,
            "timeout_s": main.timeout_s,
            "temperature": main.temperature,
        }
        inherited_summary.update(_mapping(raw_summary))
        summary = _provider_with_env(inherited_summary, env, "AGENT_SUMMARY")

    agent_data = _mapping(raw.get("agent"))
    context_data = _mapping(raw.get("context"))
    storage_data = _mapping(raw.get("storage"))
    server_data = _mapping(raw.get("server"))
    workspace_data = _mapping(raw.get("workspace"))

    sqlite_value = env.get(
        "AGENT_SQLITE_PATH",
        _string(
            storage_data.get("sqlite_path", storage_data.get("database_path")),
            "data/agent.db",
        ),
    )
    sqlite_path = Path(sqlite_value)
    if not sqlite_path.is_absolute() and sqlite_value != ":memory:":
        sqlite_path = (root / sqlite_path).resolve()

    static_value = env.get(
        "AGENT_STATIC_DIR", _string(server_data.get("static_dir"), "web/dist")
    )
    static_path = Path(static_value)
    if not static_path.is_absolute():
        static_path = (root / static_path).resolve()

    origins_value: Any = env.get("AGENT_CORS_ORIGINS", server_data.get("cors_origins"))
    if isinstance(origins_value, str):
        origins = tuple(item.strip() for item in origins_value.split(",") if item.strip())
    elif isinstance(origins_value, (list, tuple)):
        origins = tuple(str(item) for item in origins_value)
    else:
        origins = ServerSettings().cors_origins

    redis_value = env.get("REDIS_URL", storage_data.get("redis_url"))
    config = AppConfig(
        llm=LLMSettings(main=main, summary=summary, require_user_config=require_user_config),
        agent=AgentSettings(
            max_turns=_integer(
                env.get("AGENT_MAX_TURNS", agent_data.get("max_turns")), 20
            ),
            max_tool_retries=_integer(
                env.get(
                    "AGENT_MAX_TOOL_RETRIES", agent_data.get("max_tool_retries")
                ),
                2,
                minimum=0,
            ),
            tool_timeout_s=_float(
                env.get("AGENT_TOOL_TIMEOUT_S", agent_data.get("tool_timeout_s")),
                60.0,
                minimum=0.1,
                maximum=3600.0,
            ),
            tool_result_max_chars=_integer(
                env.get(
                    "AGENT_TOOL_RESULT_MAX_CHARS",
                    agent_data.get("tool_result_max_chars"),
                ),
                65_536,
                minimum=256,
            ),
        ),
        context=ContextSettings(
            token_budget=_integer(
                env.get("AGENT_TOKEN_BUDGET", context_data.get("token_budget")),
                32_000,
            ),
            summary_trigger_ratio=_float(
                env.get(
                    "AGENT_SUMMARY_TRIGGER_RATIO",
                    context_data.get("summary_trigger_ratio"),
                ),
                0.8,
                minimum=0.1,
                maximum=1.0,
            ),
            preserve_recent_messages=_integer(
                env.get(
                    "AGENT_PRESERVE_RECENT_MESSAGES",
                    context_data.get("preserve_recent_messages"),
                ),
                10,
                minimum=1,
            ),
        ),
        storage=StorageSettings(
            sqlite_path=sqlite_path,
            redis_url=(str(redis_value) if redis_value else None),
            cache_ttl_s=_integer(
                env.get("AGENT_CACHE_TTL_S", storage_data.get("cache_ttl_s")),
                86_400,
            ),
        ),
        server=ServerSettings(
            host=_string(env.get("AGENT_HOST", server_data.get("host")), "127.0.0.1"),
            port=_integer(env.get("AGENT_PORT", server_data.get("port")), 8000),
            cors_origins=origins,
            static_dir=static_path,
            cookie_secure=_env_bool(
                env.get("AGENT_COOKIE_SECURE", server_data.get("cookie_secure")), True
            ),
        ),
        workspace=WorkspaceSettings(
            default_id=_string(
                env.get(
                    "AGENT_WORKSPACE_ID",
                    workspace_data.get("default_id", workspace_data.get("id")),
                ),
                "default",
            ),
            default_name=_string(
                env.get("AGENT_WORKSPACE_NAME", workspace_data.get("default_name")),
                "Default workspace",
            ),
            root=_resolve_path(
                root,
                env.get(
                    "AGENT_WORKSPACE_ROOT", _string(workspace_data.get("root"), ".")
                ),
            ),
        ),
        config_path=selected_path if selected_path.exists() else None,
    )
    return config


__all__ = [
    "AgentSettings",
    "AppConfig",
    "ContextSettings",
    "LLMProviderConfig",
    "LLMSettings",
    "ServerSettings",
    "StorageSettings",
    "WorkspaceSettings",
    "load_config",
]
