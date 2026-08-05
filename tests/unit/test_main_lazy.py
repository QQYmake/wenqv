"""Lazy server.main import / fail-fast contract, verified in fresh interpreters.

Importing ``server.main`` must never build the FastAPI app: with the production
config (``llm.require_user_config: true``) a missing ``AGENT_SECRET_KEY`` would
make ``create_app`` fail fast, so any eager module-level ``create_app()`` call
would break ``uvicorn server.main:app`` and plain test imports. These tests
spawn a fresh Python process to prove the contract exactly as uvicorn would
observe it, without polluting the test runner's imported modules.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

REQUIRE_USER_CONFIG_YAML = (
    "llm:\n"
    "  require_user_config: true\n"
    "  main:\n"
    "    base_url: ''\n"
    "    api_key: ''\n"
    "    model: ''\n"
)


def _run(code: str, config_path: Path, *, secret_key: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("AGENT_SECRET_KEY", None)
    env.pop("AGENT_CONFIG", None)
    env["AGENT_CONFIG"] = str(config_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    if secret_key is not None:
        env["AGENT_SECRET_KEY"] = secret_key
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=config_path.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.fixture()
def require_user_config_file(tmp_path: Path) -> Path:
    config = tmp_path / "config.yaml"
    config.write_text(REQUIRE_USER_CONFIG_YAML, encoding="utf-8")
    return config


def test_importing_server_main_builds_no_app_without_secret(
    require_user_config_file: Path,
) -> None:
    """A plain import must succeed and leave no module-level ``app`` binding."""
    result = _run(
        "import server.main\n"
        "assert 'app' not in vars(server.main), 'module-level app binding exists'\n"
        "print('imported-without-app')",
        require_user_config_file,
    )
    assert result.returncode == 0, result.stderr
    assert "imported-without-app" in result.stdout


def test_get_app_fails_fast_when_secret_missing_but_required(
    require_user_config_file: Path,
) -> None:
    """Building the app without AGENT_SECRET_KEY must raise EncryptionError."""
    result = _run(
        "import server.main\n"
        "try:\n"
        "    server.main.get_app()\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
        "    print(str(exc))\n"
        "else:\n"
        "    raise SystemExit('expected EncryptionError')",
        require_user_config_file,
    )
    assert result.returncode == 0, result.stderr
    assert "EncryptionError" in result.stdout
    assert "AGENT_SECRET_KEY" in result.stdout


def test_app_attribute_resolves_to_a_real_app_with_secret(
    require_user_config_file: Path,
) -> None:
    """With a valid key, ``server.main.app`` resolves to a working FastAPI app."""
    from cryptography.fernet import Fernet

    result = _run(
        "import server.main\n"
        "app = server.main.app\n"
        "assert app is not None\n"
        "print(type(app).__name__)",
        require_user_config_file,
        secret_key=Fernet.generate_key().decode(),
    )
    assert result.returncode == 0, result.stderr
    assert "FastAPI" in result.stdout


def test_malformed_secret_fails_fast_when_required(
    require_user_config_file: Path,
) -> None:
    """A non-Fernet AGENT_SECRET_KEY must fail at app build time, not at use time."""
    result = _run(
        "import server.main\n"
        "try:\n"
        "    server.main.get_app()\n"
        "except Exception as exc:\n"
        "    print(type(exc).__name__)\n"
        "    print(str(exc))\n"
        "else:\n"
        "    raise SystemExit('expected EncryptionError')",
        require_user_config_file,
        secret_key="definitely-not-a-fernet-key",
    )
    assert result.returncode == 0, result.stderr
    assert "EncryptionError" in result.stdout
    assert "valid Fernet key" in result.stdout
