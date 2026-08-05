"""Unit tests for the encryption layer and the UserConfigRepository."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import LLMProviderConfig, LLMSettings
from server.storage.encryption import EncryptionError, FernetCipher, KeyManager
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


def test_user_config_summary_falls_back_to_user_main_when_empty() -> None:
    """An unconfigured summary role reuses the user's main provider.

    The Settings UI promises "留空则复用主模型": when the user leaves the
    summary fields empty, the summary role must resolve to the *user's* main
    config — not to the application default.
    """

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
        await repo.upsert(
            "ws-main-only",
            main={
                "base_url": "https://user.example.com/v1",
                "api_key": "sk-user-main",
                "model": "user-model",
            },
            summary={"base_url": "", "api_key": "", "model": ""},
        )
        resolved = await repo.get_resolved("ws-main-only", default)
        # Summary falls back to the user's main values, not the app defaults.
        assert resolved.summary_base_url == "https://user.example.com/v1"
        assert resolved.summary_api_key == "sk-user-main"
        assert resolved.summary_model == "user-model"
        await store.close()

    asyncio.run(scenario())


def test_user_config_roles_are_independent_when_both_configured() -> None:
    """A configured summary role must not be contaminated by the main role."""

    import asyncio

    async def scenario():
        store = SQLiteStore(":memory:")
        await store.initialize()
        repo = UserConfigRepository(store, _cipher())
        await repo.upsert(
            "ws-both",
            main={
                "base_url": "https://main.example.com/v1",
                "api_key": "sk-main-key",
                "model": "main-model",
            },
            summary={
                "base_url": "https://summary.example.com/v1",
                "api_key": "sk-summary-key",
                "model": "summary-model",
            },
        )
        resolved = await repo.get_resolved("ws-both", LLMSettings())
        assert resolved.main_base_url == "https://main.example.com/v1"
        assert resolved.main_api_key == "sk-main-key"
        assert resolved.main_model == "main-model"
        assert resolved.summary_base_url == "https://summary.example.com/v1"
        assert resolved.summary_api_key == "sk-summary-key"
        assert resolved.summary_model == "summary-model"
        await store.close()

    asyncio.run(scenario())


def test_decrypt_with_wrong_key_raises_clear_error_without_leaking() -> None:
    """Decryption with the wrong AGENT_SECRET_KEY fails loudly but never
    includes the ciphertext token or the plaintext in the error message."""

    plaintext = "sk-top-secret-9876543210"
    token = FernetCipher(KeyManager(_key())).encrypt(plaintext)
    wrong = FernetCipher(KeyManager(_key()))
    with pytest.raises(EncryptionError) as excinfo:
        wrong.decrypt(token)
    message = str(excinfo.value)
    assert "decrypt" in message.lower()
    assert plaintext not in message
    assert token not in message


def test_decrypt_without_configured_key_fails_clearly() -> None:
    """A KeyManager without any key must fail with a clear, key-free message."""

    import os

    try:
        os.environ.pop(KeyManager.ENV_NAME, None)
        manager = KeyManager()
        assert manager.configured is False
        with pytest.raises(EncryptionError) as excinfo:
            manager.require()
        assert KeyManager.ENV_NAME in str(excinfo.value)
    finally:
        os.environ.pop(KeyManager.ENV_NAME, None)