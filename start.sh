#!/usr/bin/env bash
# Dev launcher for Blue Lake Agent (Ubuntu/Linux).
# Uses the project virtualenv explicitly. Browser-local IndexedDB owns chat
# history and provider credentials, so no server secret or database is needed.

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

if ! "$PYTHON" -c 'import fastapi, uvicorn, openai, yaml' \
    >/dev/null 2>&1; then
    echo "[INFO] Missing dependencies - installing from requirements.txt ..."
    "$PYTHON" -m pip install -r "$PROJECT_DIR/requirements.txt"
fi

HOST="${AGENT_HOST:-127.0.0.1}"
PORT="${AGENT_PORT:-8000}"

echo "[INFO] Starting Blue Lake Agent at http://${HOST}:${PORT}"
exec "$PYTHON" -m uvicorn server.main:app \
    --reload --host "$HOST" --port "$PORT"
