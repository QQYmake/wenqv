"""ClientResolver adapter: per-workspace LLM clients from user_configs + defaults.

This is the composition-side adapter that implements the framework-free
``ClientResolver`` port. It reads the encrypted per-user configuration, merges
unfilled fields with the application's default LLM config, and pools
``LLMClient`` instances by ``(base_url, api_key, model)`` so a hot workspace
does not rebuild an ``httpx``/OpenAI client on every request.

The agent core only sees the sync ``ClientResolver`` port; it never imports
this module or ``server.storage``. The HTTP layer calls ``await warm(workspace)``
once per chat request (an async DB read), after which the agent's synchronous
``get_client(role, workspace_id)`` calls hit an in-memory cache. Wiring happens
in ``server.main``.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Any, Callable

from server.agent.ports import ClientResolver, LLMClient
from server.config import LLMProviderConfig
from server.storage.user_configs import UserConfigRepository, UserLLMConfig


ClientBuilder = Callable[[LLMProviderConfig], LLMClient]


class NotConfiguredError(RuntimeError):
    """Raised when a workspace has no usable LLM configuration for a role."""


class LLMResolverAdapter(ClientResolver):
    """Per-workspace ``ClientResolver`` backed by ``UserConfigRepository``."""

    def __init__(
        self,
        repo: UserConfigRepository,
        default_llm: Any,
        *,
        builder: ClientBuilder | None = None,
        pool_size: int = 32,
        cache_size: int = 256,
    ) -> None:
        self._repo = repo
        self._default_llm = default_llm
        self._builder = builder or _default_builder
        self._pool_size = pool_size
        self._cache_size = cache_size
        # workspace_id -> resolved (plaintext) config; populated by warm().
        self._config_cache: "OrderedDict[str, UserLLMConfig]" = OrderedDict()
        # (base_url, api_key, model) -> LLMClient; LRU-capped.
        self._pool: "OrderedDict[tuple[str, str, str], LLMClient]" = OrderedDict()

    # --- async warm-up (route boundary) -----------------------------------

    async def warm(self, workspace_id: str | None) -> UserLLMConfig:
        """Read the DB once per request and cache the resolved config."""

        key = workspace_id or ""
        config = await self._repo.get_resolved(key, self._default_llm)
        self._config_cache[key] = config
        self._config_cache.move_to_end(key)
        if len(self._config_cache) > self._cache_size:
            self._config_cache.popitem(last=False)
        return config

    async def has_config_async(self, workspace_id: str | None) -> bool:
        config = await self.warm(workspace_id)
        return config.is_complete("main")

    # --- sync port (agent core) ------------------------------------------

    def has_config(self, workspace_id: str | None = None) -> bool:
        key = workspace_id or ""
        cached = self._config_cache.get(key)
        if cached is not None:
            return cached.is_complete("main")
        # No warmed cache for this workspace: answer from the default config.
        main = self._default_main()
        return bool(main and main.api_key and main.base_url and main.model)

    def get_client(self, role: str, workspace_id: str | None = None) -> LLMClient:
        key = workspace_id or ""
        config = self._config_cache.get(key)
        if config is None:
            # Fall back to the default config (no DB read in the sync path).
            config = self._default_config()
        if not config.is_complete(role):
            raise NotConfiguredError(
                f"LLM configuration for role '{role}' is incomplete for "
                f"workspace {workspace_id!r}; configure it in Settings."
            )
        return self._cached_client(role, config)

    def _default_config(self) -> UserLLMConfig:
        main = self._default_main() or LLMProviderConfig()
        summary = self._default_summary() or main
        return UserLLMConfig(
            main_base_url=main.base_url,
            main_api_key=main.api_key,
            main_model=main.model,
            summary_base_url=summary.base_url,
            summary_api_key=summary.api_key,
            summary_model=summary.model,
        )

    def _cached_client(self, role: str, config: UserLLMConfig) -> LLMClient:
        if role == "summary":
            base_url, api_key, model = (
                config.summary_base_url,
                config.summary_api_key,
                config.summary_model,
            )
        elif role == "main":
            base_url, api_key, model = (
                config.main_base_url,
                config.main_api_key,
                config.main_model,
            )
        else:
            raise ValueError(f"Unknown LLM role: {role}")
        pool_key = (base_url, api_key, model)
        client = self._pool.get(pool_key)
        if client is not None:
            self._pool.move_to_end(pool_key)
            return client
        provider = LLMProviderConfig(base_url=base_url, api_key=api_key, model=model)
        client = self._builder(provider)
        self._pool[pool_key] = client
        if len(self._pool) > self._pool_size:
            self._pool.popitem(last=False)
        return client

    def _default_main(self) -> LLMProviderConfig | None:
        try:
            return self._default_llm.for_role("main")
        except Exception:
            return getattr(self._default_llm, "main", None)

    def _default_summary(self) -> LLMProviderConfig | None:
        try:
            return self._default_llm.for_role("summary")
        except Exception:
            summary = getattr(self._default_llm, "summary", None)
            return summary or self._default_main()

    def build_client(
        self, *, base_url: str, api_key: str, model: str
    ) -> LLMClient:
        """Build (or reuse a pooled) client for an ad-hoc config.

        Used by the connection-test endpoint, which validates a submitted
        config without persisting it.
        """

        config = UserLLMConfig(
            main_base_url=base_url,
            main_api_key=api_key,
            main_model=model,
            summary_base_url=base_url,
            summary_api_key=api_key,
            summary_model=model,
        )
        return self._cached_client("main", config)


def _default_builder(provider: LLMProviderConfig) -> LLMClient:
    from server.agent.llm import LLMConfig, OpenAICompatClient

    config = LLMConfig(
        base_url=provider.base_url,
        api_key=provider.api_key,
        model=provider.model,
        max_tokens=provider.max_tokens,
        temperature=provider.temperature,
        timeout_s=provider.timeout_s,
    )
    return OpenAICompatClient(config)


__all__ = ["LLMResolverAdapter", "NotConfiguredError"]