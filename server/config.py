"""Non-secret application configuration.

Conversation state and provider credentials deliberately do not belong in
server configuration.  They are supplied for the lifetime of a single browser
request and are kept by the browser in IndexedDB instead.
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


def _string_tuple(value: Any, default: tuple[str, ...] = ()) -> tuple[str, ...]:
    if value is None:
        return default
    items = value.split(",") if isinstance(value, str) else value
    if not isinstance(items, (list, tuple)):
        return default
    result: list[str] = []
    for item in items:
        name = str(item).strip()
        if name and name not in result:
            result.append(name)
    return tuple(result)


def _resolve_path(root: Path, value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (root / path).resolve()


@dataclass(slots=True)
class AgentSettings:
    max_turns: int = 20
    max_tool_retries: int = 2
    tool_timeout_s: float = 60.0
    tool_result_max_chars: int = 65_536
    default_skills: tuple[str, ...] = ()


@dataclass(slots=True)
class ContextSettings:
    token_budget: int = 32_000
    summary_trigger_ratio: float = 0.8
    preserve_recent_messages: int = 10


@dataclass(slots=True)
class ServerSettings:
    host: str = "127.0.0.1"
    port: int = 8000
    cors_origins: tuple[str, ...] = (
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    )
    static_dir: Path = Path("web/dist")


@dataclass(slots=True)
class WorkspaceSettings:
    root: Path = Path(".")
    # Trusted Skill instructions stay separate from user-writable workspaces.
    skills_root: Path | None = None


@dataclass(slots=True)
class AppConfig:
    agent: AgentSettings = field(default_factory=AgentSettings)
    context: ContextSettings = field(default_factory=ContextSettings)
    server: ServerSettings = field(default_factory=ServerSettings)
    workspace: WorkspaceSettings = field(default_factory=WorkspaceSettings)
    config_path: Path | None = field(default=None, repr=False)

    def public_dict(self) -> dict[str, Any]:
        """Return only browser-safe, non-user-specific settings."""

        return {
            "limits": {
                "max_turns": self.agent.max_turns,
                "tool_timeout_s": self.agent.tool_timeout_s,
                "tool_result_max_chars": self.agent.tool_result_max_chars,
                "token_budget": self.context.token_budget,
                "preserve_recent_messages": self.context.preserve_recent_messages,
            },
            "features": {"streaming": True, "skills": True, "abort": True},
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


_LEGACY_ENVIRONMENT = (
    "AGENT_SECRET_KEY",
    "AGENT_REQUIRE_USER_CONFIG",
    "AGENT_MAIN_API_KEY",
    "AGENT_MAIN_BASE_URL",
    "AGENT_MAIN_MODEL",
    "AGENT_SUMMARY_API_KEY",
    "AGENT_SUMMARY_BASE_URL",
    "AGENT_SUMMARY_MODEL",
    "AGENT_SQLITE_PATH",
    "REDIS_URL",
    "AGENT_CACHE_TTL_S",
    "AGENT_COOKIE_SECURE",
)


def _reject_legacy_persistence(raw: Mapping[str, Any], env: Mapping[str, str]) -> None:
    """Fail closed instead of silently reviving the old server data path."""

    legacy_sections = {key for key in ("llm", "storage") if key in raw}
    legacy_environment = [key for key in _LEGACY_ENVIRONMENT if env.get(key)]
    if legacy_sections or legacy_environment:
        raise ValueError(
            "Legacy server-side chat persistence or provider configuration is not "
            "supported. Remove llm/storage settings and AGENT_* provider, secret, "
            "SQLite, Redis, or cookie variables. Provider settings are local to the browser."
        )


def load_config(
    path: str | Path | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> AppConfig:
    """Load operational settings without accepting credentials or chat storage."""

    env = os.environ if environ is None else environ
    selected_path = Path(path or env.get("AGENT_CONFIG", "config.yaml")).resolve()
    root = selected_path.parent
    raw = _load_yaml(selected_path)
    _reject_legacy_persistence(raw, env)

    agent_data = _mapping(raw.get("agent"))
    context_data = _mapping(raw.get("context"))
    server_data = _mapping(raw.get("server"))
    workspace_data = _mapping(raw.get("workspace"))

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

    return AppConfig(
        agent=AgentSettings(
            max_turns=_integer(env.get("AGENT_MAX_TURNS", agent_data.get("max_turns")), 20),
            max_tool_retries=_integer(
                env.get("AGENT_MAX_TOOL_RETRIES", agent_data.get("max_tool_retries")),
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
                env.get("AGENT_TOOL_RESULT_MAX_CHARS", agent_data.get("tool_result_max_chars")),
                65_536,
                minimum=256,
            ),
            default_skills=_string_tuple(
                env.get("AGENT_DEFAULT_SKILLS", agent_data.get("default_skills"))
            ),
        ),
        context=ContextSettings(
            token_budget=_integer(
                env.get("AGENT_TOKEN_BUDGET", context_data.get("token_budget")),
                32_000,
            ),
            summary_trigger_ratio=_float(
                env.get("AGENT_SUMMARY_TRIGGER_RATIO", context_data.get("summary_trigger_ratio")),
                0.8,
                minimum=0.1,
                maximum=1.0,
            ),
            preserve_recent_messages=_integer(
                env.get("AGENT_PRESERVE_RECENT_MESSAGES", context_data.get("preserve_recent_messages")),
                10,
            ),
        ),
        server=ServerSettings(
            host=_string(env.get("AGENT_HOST", server_data.get("host")), "127.0.0.1"),
            port=_integer(env.get("AGENT_PORT", server_data.get("port")), 8000),
            cors_origins=origins,
            static_dir=static_path,
        ),
        workspace=WorkspaceSettings(
            root=_resolve_path(
                root,
                env.get("AGENT_WORKSPACE_ROOT", _string(workspace_data.get("root"), ".")),
            ),
            skills_root=(
                _resolve_path(
                    root,
                    env.get("AGENT_SKILLS_ROOT", _string(workspace_data.get("skills_root"), "skills")),
                )
                if env.get("AGENT_SKILLS_ROOT", workspace_data.get("skills_root")) is not None
                else None
            ),
        ),
        config_path=selected_path if selected_path.exists() else None,
    )


__all__ = [
    "AgentSettings",
    "AppConfig",
    "ContextSettings",
    "ServerSettings",
    "WorkspaceSettings",
    "load_config",
]
