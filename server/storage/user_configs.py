"""Per-workspace LLM configuration repository: encrypted storage + masking.

Owns the user_configs concern end-to-end: it encrypts API keys on write,
decrypts on read for resolver use, and returns masked (``sk-***xxx``) views
for the HTTP layer so plaintext keys never leave the server. It also merges a
per-field fallback onto the application's default LLM config so a user can fill
only the fields they want to override.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from server.config import LLMProviderConfig

from .encryption import FernetCipher
from .sqlite import SQLiteStore


@dataclass(slots=True)
class UserLLMConfig:
    """Resolved per-workspace LLM configuration (plaintext, in-memory only)."""

    main_base_url: str = ""
    main_api_key: str = ""
    main_model: str = ""
    summary_base_url: str = ""
    summary_api_key: str = ""
    summary_model: str = ""

    def is_complete(self, role: str) -> bool:
        if role == "main":
            return bool(self.main_base_url and self.main_api_key and self.main_model)
        if role == "summary":
            return bool(
                self.summary_base_url and self.summary_api_key and self.summary_model
            )
        raise ValueError(f"Unknown LLM role: {role}")


def mask_api_key(key: str) -> str:
    """Return a non-sensitive view of an API key for the browser."""

    if not key:
        return ""
    if len(key) <= 8:
        return "***"
    return f"{key[:3]}***{key[-3:]}"


class UserConfigRepository:
    """Encrypts on write, decrypts on read, and merges defaults for the resolver."""

    def __init__(self, store: SQLiteStore, cipher: FernetCipher) -> None:
        self._store = store
        self._cipher = cipher

    async def get_raw(self, workspace_id: str) -> dict[str, str] | None:
        return await self._store.get_user_config(workspace_id)

    async def get_resolved(
        self, workspace_id: str, default_llm: Any
    ) -> UserLLMConfig:
        """Return the user config with per-field fallback to ``default_llm``.

        ``default_llm`` is an :class:`LLMSettings`-like object exposing
        ``main`` and ``summary`` (which may be ``None``).
        """

        row = await self._store.get_user_config(workspace_id)
        main_default = _provider(default_llm, "main")
        summary_default = _provider(default_llm, "summary") or main_default

        def pick(user: str, fallback: str) -> str:
            return user.strip() if user and user.strip() else (fallback or "")

        if row is None:
            return UserLLMConfig(
                main_base_url=main_default.base_url,
                main_api_key=main_default.api_key,
                main_model=main_default.model,
                summary_base_url=summary_default.base_url,
                summary_api_key=summary_default.api_key,
                summary_model=summary_default.model,
            )

        return UserLLMConfig(
            main_base_url=pick(row["main_base_url"], main_default.base_url),
            main_api_key=self._cipher.decrypt(row["main_api_key_encrypted"])
            or main_default.api_key,
            main_model=pick(row["main_model"], main_default.model),
            summary_base_url=pick(row["summary_base_url"], summary_default.base_url),
            summary_api_key=self._cipher.decrypt(row["summary_api_key_encrypted"])
            or summary_default.api_key,
            summary_model=pick(row["summary_model"], summary_default.model),
        )

    async def get_masked(self, workspace_id: str) -> dict[str, Any]:
        """Return the browser-safe view: masked keys, never plaintext."""

        row = await self._store.get_user_config(workspace_id)
        if row is None:
            return {
                "main": {"base_url": "", "api_key": "", "model": ""},
                "summary": {"base_url": "", "api_key": "", "model": ""},
                "has_config": False,
            }
        return {
            "main": {
                "base_url": row["main_base_url"],
                "api_key": mask_api_key(self._cipher.decrypt(row["main_api_key_encrypted"])),
                "model": row["main_model"],
            },
            "summary": {
                "base_url": row["summary_base_url"],
                "api_key": mask_api_key(self._cipher.decrypt(row["summary_api_key_encrypted"])),
                "model": row["summary_model"],
            },
            "has_config": True,
        }

    async def upsert(
        self,
        workspace_id: str,
        *,
        main: dict[str, str],
        summary: dict[str, str],
    ) -> dict[str, Any]:
        """Persist the user config.

        Empty ``api_key`` keeps the previously stored key (so a masked round-trip
        from the UI does not wipe the key). A non-empty key is re-encrypted.
        """

        existing = await self._store.get_user_config(workspace_id)
        main_cipher = _persisted_key(
            self._cipher, main.get("api_key", ""), existing, "main_api_key_encrypted"
        )
        summary_cipher = _persisted_key(
            self._cipher,
            summary.get("api_key", ""),
            existing,
            "summary_api_key_encrypted",
        )
        return await self._store.upsert_user_config(
            workspace_id,
            main_base_url=str(main.get("base_url", "")).strip(),
            main_api_key_encrypted=main_cipher,
            main_model=str(main.get("model", "")).strip(),
            summary_base_url=str(summary.get("base_url", "")).strip(),
            summary_api_key_encrypted=summary_cipher,
            summary_model=str(summary.get("model", "")).strip(),
        )


def _persisted_key(
    cipher: FernetCipher,
    submitted: str,
    existing: dict[str, str] | None,
    column: str,
) -> str:
    """Return the ciphertext to persist for an api_key field.

    A non-empty submission is freshly encrypted. An empty submission keeps the
    previously stored ciphertext untouched (so the masked round-trip from the
    UI never wipes an existing key). There is no double encryption: we never
    re-encrypt an existing ciphertext.
    """

    if submitted:
        return cipher.encrypt(submitted)
    if existing is None:
        return ""
    return existing.get(column, "")


def _provider(default_llm: Any, role: str) -> LLMProviderConfig:
    getter = getattr(default_llm, "for_role", None)
    if callable(getter):
        try:
            return getter(role)
        except ValueError:
            return LLMProviderConfig()
    if role == "main":
        return getattr(default_llm, "main", LLMProviderConfig()) or LLMProviderConfig()
    return getattr(default_llm, "summary", None) or LLMProviderConfig()


__all__ = ["UserConfigRepository", "UserLLMConfig", "mask_api_key"]