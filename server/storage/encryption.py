"""Fernet-based encryption for user-supplied API keys.

The key never lives in config.yaml; it comes from the ``AGENT_SECRET_KEY``
environment variable (wired in change 3). A missing key in production must
fail fast at startup; the ``KeyManager`` here keeps that policy decision in the
composition root. This module only performs the symmetric encrypt/decrypt and
key loading so it stays trivially unit-testable.
"""

from __future__ import annotations

import os
from typing import Any


class EncryptionError(RuntimeError):
    """Raised when encryption/decryption fails or no key is configured."""


class KeyManager:
    """Loads the Fernet key from the environment, with an optional dev fallback.

    Production wiring (change 3) calls ``require()`` to fail fast when the key
    is missing. Tests inject a key directly via the constructor.
    """

    ENV_NAME = "AGENT_SECRET_KEY"

    def __init__(self, key: str | bytes | None = None) -> None:
        if key is not None:
            self._key = key.encode() if isinstance(key, str) else bytes(key)
        else:
            raw = os.environ.get(self.ENV_NAME)
            self._key = raw.encode() if raw else None

    @property
    def configured(self) -> bool:
        return self._key is not None

    def require(self) -> bytes:
        if self._key is None:
            raise EncryptionError(
                f"{self.ENV_NAME} is not set; cannot encrypt user API keys"
            )
        return self._key


class FernetCipher:
    """Thin wrapper around :mod:`cryptography.fernet` with a stable interface."""

    def __init__(self, key_manager: KeyManager) -> None:
        self._key_manager = key_manager

    def _fernet(self) -> Any:
        try:
            from cryptography.fernet import Fernet
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise EncryptionError("cryptography is required for key encryption") from exc
        return Fernet(self._key_manager.require())

    def encrypt(self, plaintext: str) -> str:
        if not plaintext:
            return ""
        token = self._fernet().encrypt(plaintext.encode("utf-8"))
        return token.decode("ascii")

    def decrypt(self, token: str) -> str:
        if not token:
            return ""
        try:
            return self._fernet().decrypt(token.encode("ascii")).decode("utf-8")
        except Exception as exc:  # invalid token / wrong key
            raise EncryptionError("Could not decrypt stored API key") from exc


__all__ = ["EncryptionError", "FernetCipher", "KeyManager"]