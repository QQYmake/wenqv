#!/usr/bin/env bash
# Dev launcher for Blue Lake Agent (Ubuntu/Linux).
# Uses the project virtualenv explicitly and keeps the local Fernet key
# stable across restarts so saved model configuration remains readable.

set -euo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$PROJECT_DIR"

PYTHON="$PROJECT_DIR/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "[ERROR] Virtualenv not found at $PYTHON" >&2
    echo "Create and install it first:" >&2
    echo "  python3 -m venv .venv" >&2
    echo "  .venv/bin/python -m pip install -r requirements-dev.txt" >&2
    exit 1
fi

if ! "$PYTHON" -c 'import fastapi, uvicorn, openai, cryptography, yaml, aiosqlite' \
    >/dev/null 2>&1; then
    echo "[INFO] Missing dependencies - installing from requirements.txt ..."
    "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

SECRET_FILE="$PROJECT_DIR/.agent_secret_key"
if [[ -z "${AGENT_SECRET_KEY:-}" ]]; then
    if [[ -s "$SECRET_FILE" ]]; then
        AGENT_SECRET_KEY="$(<"$SECRET_FILE")"
    else
        echo "[INFO] Generating local .agent_secret_key ..."
        AGENT_SECRET_KEY="$("$PYTHON" -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
        (
            umask 077
            printf '%s\n' "$AGENT_SECRET_KEY" > "$SECRET_FILE"
        )
    fi
fi

export AGENT_SECRET_KEY
export AGENT_COOKIE_SECURE="${AGENT_COOKIE_SECURE:-false}"

HOST="${AGENT_HOST:-127.0.0.1}"
PORT="${AGENT_PORT:-8000}"

echo "[INFO] Starting Blue Lake Agent at http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn server.main:app \
    --reload --host "$HOST" --port "$PORT"
