"""Unit tests for config.yaml guards: require_user_config + AGENT_SECRET_KEY."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.config import LLMSettings, load_config
from server.main import _build_cipher
from server.storage.encryption import EncryptionError


def test_load_config_supports_require_user_config_flag(tmp_path: Path) -> None:
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "llm:\n  require_user_config: true\n  main:\n    base_url: ''\n    api_key: ''\n    model: ''\n",
        encoding="utf-8",
    )
    config = load_config(config_file, environ={})
    assert config.llm.require_user_config is True

    config2 = load_config(
        tmp_path / "none.yaml", environ={"AGENT_REQUIRE_USER_CONFIG": "true"}
    )
    assert config2.llm.require_user_config is True


def test_load_config_require_user_config_defaults_false() -> None:
    config = load_config(Path("does-not-exist.yaml"), environ={})
    assert config.llm.require_user_config is False


def test_build_cipher_fails_fast_when_required_and_no_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)
    config = load_config(Path("does-not-exist.yaml"), environ={})
    config.llm.require_user_config = True  # type: ignore[misc]
    with pytest.raises(EncryptionError, match="AGENT_SECRET_KEY"):
        _build_cipher(config)


def test_build_cipher_uses_ephemeral_key_when_not_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AGENT_SECRET_KEY", raising=False)
    config = load_config(Path("does-not-exist.yaml"), environ={})
    # require_user_config defaults to False -> no fail-fast, ephemeral key.
    cipher = _build_cipher(config)
    # Round-trip works with the ephemeral key.
    token = cipher.encrypt("sk-test")
    assert cipher.decrypt(token) == "sk-test"


def test_build_cipher_uses_env_secret_when_required(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cryptography.fernet import Fernet

    key = Fernet.generate_key().decode()
    monkeypatch.setenv("AGENT_SECRET_KEY", key)
    config = load_config(Path("does-not-exist.yaml"), environ={})
    config.llm.require_user_config = True  # type: ignore[misc]
    cipher = _build_cipher(config)
    assert cipher.decrypt(cipher.encrypt("sk-x")) == "sk-x"


def test_module_app_attribute_resolves_lazily_and_is_never_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``uvicorn server.main:app`` must resolve through __getattr__, not None.

    A module-level ``app = None`` binding shadows ``__getattr__``: uvicorn's
    import-string resolution then hands the server a None app and every request
    crashes with 500 (TypeError: 'NoneType' object is not callable).
    """

    import server.main as main_module

    # Simulate a fresh interpreter: no pre-existing module attribute.
    if "app" in vars(main_module):
        monkeypatch.delattr(main_module, "app")

    sentinel = object()
    monkeypatch.setattr(main_module, "get_app", lambda: sentinel)

    # This is exactly what uvicorn does for `server.main:app`.
    assert getattr(main_module, "app") is sentinel
    # The resolved value must never be None.
    assert sentinel is not None