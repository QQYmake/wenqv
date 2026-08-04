"""Unit tests for the encryption layer and the UserConfigRepository."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import LLMProviderConfig, LLMSettings
from server.storage.encryption import FernetCipher, KeyManager
from server.storage.sqlite import SQLiteStore
from server.storage.user_configs import (
    UserConfigRepository,
    UserLLMConfig,
    mask_api_key,
)


def _key() -> str:
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


def _cipher() -> FernetCipher:
    return FernetCipher(KeyManager(_key()))


def test_encryption_roundtrip_for_api_key() -> None:
    cipher = _cipher()
    token = cipher.encrypt("sk-live-1234567890")
    assert token != "sk-live-1234567890"
    assert cipher.decrypt(token) == "sk-live-1234567890"
    # Empty input round-trips as empty.
    assert cipher.encrypt("") == ""
    assert cipher.decrypt("") == ""


def test_encryption_wrong_key_fails() -> None:
    token = FernetCipher(KeyManager(_key())).encrypt("sk-x")
    other = FernetCipher(KeyManager(_key()))
    with pytest.raises(Exception):
        other.decrypt(token)


def test_mask_api_key_never_reveals_plaintext() -> None:
    assert mask_api_key("sk-abcdef1234567890") == "sk-***890"
    assert mask_api_key("short") == "***"
    assert mask_api_key("") == ""


def test_user_config_persists_and_reads_back_masked_key() -> None:
    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        await repo.upsert(
            "ws-1",
            main={
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-supersecret-12345",
                "model": "gpt-test",
            },
            summary={
                "base_url": "",
                "api_key": "",
                "model": "",
            },
        )

        # The raw row stores ciphertext, never plaintext.
        raw = await store.get_user_config("ws-1")
        assert raw["main_api_key_encrypted"] != "sk-supersecret-12345"
        assert "sk-supersecret" not in raw["main_api_key_encrypted"]

        # The masked view hides the key but shows it is configured.
        masked = await repo.get_masked("ws-1")
        assert masked["has_config"] is True
        assert masked["main"]["model"] == "gpt-test"
        assert masked["main"]["api_key"] == "sk-***345"
        assert masked["main"]["base_url"] == "https://api.example.com/v1"
        # Summary fell back to empty (no default supplied here).
        assert masked["summary"]["api_key"] == ""

        # The resolver-facing resolved config carries plaintext (in-memory only).
        resolved = await repo.get_resolved("ws-1", LLMSettings())
        assert resolved.main_api_key == "sk-supersecret-12345"
        await store.close()

    asyncio.run(scenario())


def test_masked_has_config_false_when_only_summary_saved() -> None:
    """A summary-only row does not unlock chat: has_config tracks main."""

    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        await repo.upsert(
            "ws-summary-only",
            main={"base_url": "", "api_key": "", "model": ""},
            summary={
                "base_url": "https://summary.example.com/v1",
                "api_key": "sk-summary-1",
                "model": "summary-model",
            },
        )
        masked = await repo.get_masked("ws-summary-only")
        assert masked["has_config"] is False
        assert masked["summary"]["api_key"] == "sk-***y-1"
        resolved = await repo.get_resolved("ws-summary-only", LLMSettings())
        assert resolved.is_complete("main") is False
        assert resolved.is_complete("summary") is True
        await store.close()

    asyncio.run(scenario())


def test_undecryptable_stored_key_is_treated_as_empty() -> None:
    """After a secret rotation, stale ciphertext must not 500; it reads as empty."""

    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        await repo.upsert(
            "ws-stale",
            main={
                "base_url": "https://api.example.com/v1",
                "api_key": "sk-stale-key-123456",
                "model": "gpt-test",
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )

        # A fresh key (e.g. after restart with a new secret) cannot decrypt.
        rotated = UserConfigRepository(store, _cipher())
        masked = await rotated.get_masked("ws-stale")
        assert masked["has_config"] is False
        assert masked["main"]["api_key"] == ""
        resolved = await rotated.get_resolved("ws-stale", LLMSettings())
        assert resolved.main_api_key == ""
        assert resolved.is_complete("main") is False
        await store.close()

    asyncio.run(scenario())


def test_user_config_empty_api_key_keeps_stored_key() -> None:
    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        await repo.upsert(
            "ws-1",
            main={"base_url": "https://a", "api_key": "sk-original", "model": "m"},
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        # Re-PUT with an empty api_key (e.g. the masked round-trip from the UI).
        await repo.upsert(
            "ws-1",
            main={"base_url": "https://a", "api_key": "", "model": "m"},
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        resolved = await repo.get_resolved("ws-1", LLMSettings())
        assert resolved.main_api_key == "sk-original"
        await store.close()

    asyncio.run(scenario())


def test_user_config_falls_back_to_default_when_field_missing() -> None:
    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        default = LLMSettings(
            main=LLMProviderConfig(
                base_url="https://default.example.com/v1",
                api_key="sk-default",
                model="default-model",
            )
        )
        # No user config at all -> default used.
        resolved = await repo.get_resolved("ws-new", default)
        assert resolved.main_api_key == "sk-default"
        assert resolved.main_model == "default-model"

        # Partial user config: filled fields win, empty fields fall back.
        await repo.upsert(
            "ws-new",
            main={
                "base_url": "https://user.example.com/v1",
                "api_key": "sk-user",
                "model": "",
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        resolved = await repo.get_resolved("ws-new", default)
        assert resolved.main_base_url == "https://user.example.com/v1"
        assert resolved.main_api_key == "sk-user"
        assert resolved.main_model == "default-model"
        await store.close()

    asyncio.run(scenario())