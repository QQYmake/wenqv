"""Deployment guard: SQLiteStore must run in WAL mode for file databases."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from server.storage import SQLiteStore


def test_sqlite_store_uses_wal_mode(tmp_path: Path) -> None:
    db_path = tmp_path / "agent.db"
    store = SQLiteStore(db_path)
    import asyncio

    async def scenario():
        await store.initialize()
        # Force a write so the WAL files exist and the journal mode is active.
        await store.ensure_workspace("ws", "WS")
        await store.close()

    asyncio.run(scenario())

    connection = sqlite3.connect(str(db_path))
    try:
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        connection.close()
    assert mode.lower() == "wal"