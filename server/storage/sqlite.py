"""Async-friendly SQLite persistence for workspaces, sessions, and messages."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, TypeVar
import asyncio
import json
import sqlite3
import uuid

from .cache import AsyncCache, MemoryTTLCache, SideCache


T = TypeVar("T")


SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS workspaces (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_sessions_workspace_updated
ON sessions(workspace_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'message',
    name TEXT,
    content_json TEXT NOT NULL,
    metadata_json TEXT NOT NULL DEFAULT '{}',
    token_count INTEGER,
    created_at TEXT NOT NULL,
    UNIQUE(session_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_messages_session_sequence
ON messages(session_id, sequence);

CREATE TABLE IF NOT EXISTS session_skills (
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    message_id TEXT NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
    loaded_at TEXT NOT NULL,
    PRIMARY KEY(session_id, skill_name)
);

CREATE INDEX IF NOT EXISTS idx_session_skills_message
ON session_skills(message_id);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _json_load(value: str | None, default: Any) -> Any:
    if value is None:
        return default
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return default


def _session_dict(row: sqlite3.Row) -> dict[str, Any]:
    result = {
        "id": row["id"],
        "workspace_id": row["workspace_id"],
        "title": row["title"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }
    if "message_count" in row.keys():
        result["message_count"] = int(row["message_count"] or 0)
    return result


def _message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row["session_id"],
        "sequence": int(row["sequence"]),
        "role": row["role"],
        "kind": row["kind"],
        "name": row["name"],
        "content": _json_load(row["content_json"], ""),
        "metadata": _json_load(row["metadata_json"], {}),
        "token_count": row["token_count"],
        "created_at": row["created_at"],
    }


class SQLiteStore:
    """A single-process repository using short-lived SQLite connections.

    Synchronous sqlite calls run through ``asyncio.to_thread``.  Writes are
    serialized by a process-local lock; SQLite still enforces correctness when
    more than one process opens the same database.
    """

    def __init__(self, path: str | Path, cache: AsyncCache | None = None):
        raw_path = str(path)
        self._keeper: sqlite3.Connection | None = None
        if raw_path == ":memory:":
            self.path = f"file:agent-chat-{uuid.uuid4().hex}?mode=memory&cache=shared"
            self._uri = True
        else:
            self.path = str(Path(raw_path).resolve())
            self._uri = self.path.startswith("file:")
        self.cache = cache or SideCache(MemoryTTLCache())
        self._write_lock = asyncio.Lock()
        self._init_lock = asyncio.Lock()
        self._initialized = False

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5.0,
            isolation_level=None,
            check_same_thread=False,
            uri=self._uri,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    async def initialize(self) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            if not self._uri:
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)

            def initialize_sync() -> None:
                if self.path.startswith("file:agent-chat-"):
                    self._keeper = self._connect()
                    connection = self._keeper
                else:
                    connection = self._connect()
                try:
                    try:
                        connection.execute("PRAGMA journal_mode = WAL")
                    except sqlite3.DatabaseError:
                        pass
                    connection.executescript(SCHEMA)
                    connection.execute(
                        "INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                        (_now(),),
                    )
                finally:
                    if connection is not self._keeper:
                        connection.close()

            await asyncio.to_thread(initialize_sync)
            self._initialized = True

    async def _read(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        await self.initialize()

        def run() -> T:
            connection = self._connect()
            try:
                return operation(connection)
            finally:
                connection.close()

        return await asyncio.to_thread(run)

    async def _write(self, operation: Callable[[sqlite3.Connection], T]) -> T:
        await self.initialize()
        async with self._write_lock:
            def run() -> T:
                connection = self._connect()
                try:
                    connection.execute("BEGIN IMMEDIATE")
                    result = operation(connection)
                    connection.commit()
                    return result
                except BaseException:
                    connection.rollback()
                    raise
                finally:
                    connection.close()

            return await asyncio.to_thread(run)

    async def close(self) -> None:
        await self.cache.close()
        if self._keeper is not None:
            self._keeper.close()
            self._keeper = None
        self._initialized = False

    async def ensure_workspace(self, workspace_id: str, name: str | None = None) -> dict[str, Any]:
        workspace_id = workspace_id.strip()
        if not workspace_id:
            raise ValueError("workspace_id cannot be blank")
        timestamp = _now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                """
                INSERT INTO workspaces(id, name, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (workspace_id, name or workspace_id, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT * FROM workspaces WHERE id = ?", (workspace_id,)
            ).fetchone()
            return dict(row)

        return await self._write(operation)

    async def create_session(
        self,
        workspace_id: str,
        title: str = "New conversation",
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        workspace_id = workspace_id.strip()
        title = title.strip() or "New conversation"
        identifier = session_id or str(uuid.uuid4())
        timestamp = _now()

        def operation(connection: sqlite3.Connection) -> dict[str, Any]:
            connection.execute(
                """
                INSERT INTO workspaces(id, name, created_at, updated_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(id) DO NOTHING
                """,
                (workspace_id, workspace_id, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO sessions(id, workspace_id, title, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (identifier, workspace_id, title, timestamp, timestamp),
            )
            row = connection.execute(
                "SELECT *, 0 AS message_count FROM sessions WHERE id = ?", (identifier,)
            ).fetchone()
            return _session_dict(row)

        result = await self._write(operation)
        await self.cache.delete_prefix(f"sessions:{workspace_id}:")
        return result

    async def get_session(
        self, session_id: str, workspace_id: str | None = None
    ) -> dict[str, Any] | None:
        workspace_key = workspace_id or "*"
        key = f"session:{workspace_key}:{session_id}"
        cached = await self.cache.get(key)
        if cached is not None:
            return cached

        def operation(connection: sqlite3.Connection) -> dict[str, Any] | None:
            sql = """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                       AS message_count
                FROM sessions s WHERE s.id = ?
            """
            params: list[Any] = [session_id]
            if workspace_id is not None:
                sql += " AND s.workspace_id = ?"
                params.append(workspace_id)
            row = connection.execute(sql, params).fetchone()
            return _session_dict(row) if row else None

        result = await self._read(operation)
        if result is not None:
            await self.cache.set(key, result)
        return result

    async def list_sessions(
        self, workspace_id: str, *, limit: int = 100, offset: int = 0
    ) -> list[dict[str, Any]]:
        limit = min(max(1, int(limit)), 500)
        offset = max(0, int(offset))
        key = f"sessions:{workspace_id}:{limit}:{offset}"
        cached = await self.cache.get(key)
        if cached is not None:
            return cached

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = connection.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                       AS message_count
                FROM sessions s
                WHERE s.workspace_id = ?
                ORDER BY s.updated_at DESC, s.id DESC
                LIMIT ? OFFSET ?
                """,
                (workspace_id, limit, offset),
            ).fetchall()
            return [_session_dict(row) for row in rows]

        result = await self._read(operation)
        await self.cache.set(key, result)
        return result

    async def rename_session(
        self, session_id: str, title: str, workspace_id: str | None = None
    ) -> dict[str, Any] | None:
        title = title.strip()
        if not title:
            raise ValueError("title cannot be blank")
        timestamp = _now()

        def operation(connection: sqlite3.Connection) -> tuple[dict[str, Any] | None, str | None]:
            sql = "UPDATE sessions SET title = ?, updated_at = ? WHERE id = ?"
            params: list[Any] = [title, timestamp, session_id]
            if workspace_id is not None:
                sql += " AND workspace_id = ?"
                params.append(workspace_id)
            cursor = connection.execute(sql, params)
            if cursor.rowcount == 0:
                return None, None
            row = connection.execute(
                """
                SELECT s.*,
                       (SELECT COUNT(*) FROM messages m WHERE m.session_id = s.id)
                       AS message_count
                FROM sessions s WHERE s.id = ?
                """,
                (session_id,),
            ).fetchone()
            return _session_dict(row), row["workspace_id"]

        result, actual_workspace = await self._write(operation)
        if actual_workspace:
            await self._invalidate_session(actual_workspace, session_id)
        return result

    async def delete_session(self, session_id: str, workspace_id: str | None = None) -> bool:
        def operation(connection: sqlite3.Connection) -> tuple[bool, str | None]:
            row = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if row is None or (workspace_id is not None and row["workspace_id"] != workspace_id):
                return False, None
            connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
            return True, row["workspace_id"]

        deleted, actual_workspace = await self._write(operation)
        if actual_workspace:
            await self._invalidate_session(actual_workspace, session_id)
            await self.cache.delete_prefix(f"messages:{session_id}:")
        return deleted

    async def add_message(
        self,
        session_id: str,
        role: str,
        content: Any,
        *,
        kind: str = "message",
        name: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        token_count: int | None = None,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> dict[str, Any]:
        identifier = message_id or str(uuid.uuid4())
        timestamp = created_at or _now()

        def operation(connection: sqlite3.Connection) -> tuple[dict[str, Any], str]:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"Session not found: {session_id}")
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            connection.execute(
                """
                INSERT INTO messages(
                    id, session_id, sequence, role, kind, name, content_json,
                    metadata_json, token_count, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identifier,
                    session_id,
                    sequence,
                    role,
                    kind,
                    name,
                    _json_dump(content),
                    _json_dump(dict(metadata or {})),
                    token_count,
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id)
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (identifier,)
            ).fetchone()
            return _message_dict(row), session["workspace_id"]

        result, workspace_id = await self._write(operation)
        await self._invalidate_after_message(workspace_id, session_id)
        return result

    async def add_messages(
        self, session_id: str, messages: Iterable[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        for message in messages:
            results.append(
                await self.add_message(
                    session_id,
                    str(message.get("role", "user")),
                    message.get("content", ""),
                    kind=str(message.get("kind", "message")),
                    name=message.get("name"),
                    metadata=_mapping_value(message.get("metadata")),
                    token_count=message.get("token_count"),
                    message_id=message.get("id"),
                )
            )
        return results

    async def list_messages(
        self,
        session_id: str,
        workspace_id: str | None = None,
        *,
        limit: int | None = None,
        before_sequence: int | None = None,
    ) -> list[dict[str, Any]] | None:
        normalized_limit = min(max(1, int(limit)), 5_000) if limit else None
        key = f"messages:{session_id}:{workspace_id or '*'}:{normalized_limit}:{before_sequence}"
        cached = await self.cache.get(key)
        if cached is not None:
            return cached

        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]] | None:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None or (
                workspace_id is not None and session["workspace_id"] != workspace_id
            ):
                return None
            where = "session_id = ?"
            params: list[Any] = [session_id]
            if before_sequence is not None:
                where += " AND sequence < ?"
                params.append(before_sequence)
            if normalized_limit is None:
                rows = connection.execute(
                    f"SELECT * FROM messages WHERE {where} ORDER BY sequence ASC", params
                ).fetchall()
            else:
                params.append(normalized_limit)
                rows = connection.execute(
                    f"""
                    SELECT * FROM (
                        SELECT * FROM messages WHERE {where}
                        ORDER BY sequence DESC LIMIT ?
                    ) ORDER BY sequence ASC
                    """,
                    params,
                ).fetchall()
            return [_message_dict(row) for row in rows]

        result = await self._read(operation)
        if result is not None:
            await self.cache.set(key, result)
        return result

    async def replace_all_messages(
        self, session_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> None:
        """Atomically replace the context while rebuilding active-skill links."""

        timestamp = _now()
        normalized = [dict(message) for message in messages]

        def operation(connection: sqlite3.Connection) -> str:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                raise KeyError(f"Session not found: {session_id}")
            connection.execute("DELETE FROM session_skills WHERE session_id = ?", (session_id,))
            connection.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            for sequence, message in enumerate(normalized, start=1):
                message_id = str(message.get("id") or uuid.uuid4())
                metadata = dict(message.get("metadata") or {})
                kind = str(message.get("kind") or "message")
                name = _optional_string(message.get("name"))
                connection.execute(
                    """
                    INSERT INTO messages(
                        id, session_id, sequence, role, kind, name, content_json,
                        metadata_json, token_count, created_at
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        message_id,
                        session_id,
                        sequence,
                        str(message.get("role") or "user"),
                        kind,
                        name,
                        _json_dump(message.get("content")),
                        _json_dump(metadata),
                        message.get("token_count"),
                        str(message.get("created_at") or timestamp),
                    ),
                )
                skill_name = metadata.get("skill_name")
                if kind == "skill" and skill_name:
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO session_skills(
                            session_id, skill_name, message_id, loaded_at
                        ) VALUES(?, ?, ?, ?)
                        """,
                        (session_id, str(skill_name), message_id, timestamp),
                    )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id)
            )
            return session["workspace_id"]

        workspace_id = await self._write(operation)
        await self._invalidate_after_message(workspace_id, session_id)

    async def inject_skill_message(
        self,
        session_id: str,
        skill_name: str,
        content: str,
        *,
        workspace_id: str | None = None,
        role: str = "user",
        metadata: Mapping[str, Any] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Atomically persist one skill message; returns ``(message, existed)``."""

        timestamp = _now()
        skill_name = skill_name.strip()
        if not skill_name:
            raise ValueError("skill_name cannot be blank")

        def operation(connection: sqlite3.Connection) -> tuple[dict[str, Any], bool, str]:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None or (
                workspace_id is not None and session["workspace_id"] != workspace_id
            ):
                raise KeyError(f"Session not found: {session_id}")
            existing = connection.execute(
                """
                SELECT m.* FROM session_skills ss
                JOIN messages m ON m.id = ss.message_id
                WHERE ss.session_id = ? AND ss.skill_name = ?
                """,
                (session_id, skill_name),
            ).fetchone()
            if existing is not None:
                return _message_dict(existing), True, session["workspace_id"]
            sequence = connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()[0]
            message_id = str(uuid.uuid4())
            marked_metadata = {"skill_name": skill_name, **dict(metadata or {})}
            connection.execute(
                """
                INSERT INTO messages(
                    id, session_id, sequence, role, kind, name, content_json,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, ?, 'skill', ?, ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    sequence,
                    role,
                    skill_name,
                    _json_dump(content),
                    _json_dump(marked_metadata),
                    timestamp,
                ),
            )
            connection.execute(
                """
                INSERT INTO session_skills(session_id, skill_name, message_id, loaded_at)
                VALUES(?, ?, ?, ?)
                """,
                (session_id, skill_name, message_id, timestamp),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id)
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return _message_dict(row), False, session["workspace_id"]

        result, existed, actual_workspace = await self._write(operation)
        if not existed:
            await self._invalidate_after_message(actual_workspace, session_id)
        return result, existed

    async def remove_skill(
        self, session_id: str, skill_name: str, workspace_id: str | None = None
    ) -> bool:
        def operation(connection: sqlite3.Connection) -> tuple[bool, str | None]:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None or (
                workspace_id is not None and session["workspace_id"] != workspace_id
            ):
                return False, None
            row = connection.execute(
                """
                SELECT message_id FROM session_skills
                WHERE session_id = ? AND skill_name = ?
                """,
                (session_id, skill_name),
            ).fetchone()
            if row is None:
                return False, session["workspace_id"]
            message = connection.execute(
                "SELECT role, metadata_json FROM messages WHERE id = ?", (row["message_id"],)
            ).fetchone()
            connection.execute(
                "DELETE FROM session_skills WHERE session_id = ? AND skill_name = ?",
                (session_id, skill_name),
            )
            if message is not None and message["role"] == "tool":
                metadata = _json_load(message["metadata_json"], {})
                metadata.update({"kind": "skill_removed", "redacted": True})
                connection.execute(
                    """
                    UPDATE messages SET kind = 'skill_removed', content_json = ?, metadata_json = ?
                    WHERE id = ?
                    """,
                    (
                        _json_dump(
                            _json_dump({"status": "removed", "name": skill_name})
                        ),
                        _json_dump(metadata),
                        row["message_id"],
                    ),
                )
            else:
                connection.execute("DELETE FROM messages WHERE id = ?", (row["message_id"],))
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (_now(), session_id)
            )
            return True, session["workspace_id"]

        removed, actual_workspace = await self._write(operation)
        if removed and actual_workspace:
            await self._invalidate_after_message(actual_workspace, session_id)
        return removed

    async def list_loaded_skills(
        self, session_id: str, workspace_id: str | None = None
    ) -> list[dict[str, Any]] | None:
        def operation(connection: sqlite3.Connection) -> list[dict[str, Any]] | None:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None or (
                workspace_id is not None and session["workspace_id"] != workspace_id
            ):
                return None
            rows = connection.execute(
                """
                SELECT skill_name, message_id, loaded_at FROM session_skills
                WHERE session_id = ? ORDER BY loaded_at ASC
                """,
                (session_id,),
            ).fetchall()
            return [dict(row) for row in rows]

        return await self._read(operation)

    async def add_tool_event(
        self,
        session_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> dict[str, Any]:
        if event_type not in {"tool_call", "tool_result"}:
            raise ValueError("event_type must be tool_call or tool_result")
        return await self.add_message(
            session_id,
            "assistant" if event_type == "tool_call" else "tool",
            payload.get("result", payload.get("arguments", "")),
            kind=event_type,
            name=_optional_string(payload.get("name")),
            metadata=dict(payload),
        )

    async def compact_messages(
        self,
        session_id: str,
        message_ids: Sequence[str],
        summary_content: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Replace selected early messages with one persisted summary message."""

        unique_ids = list(dict.fromkeys(message_ids))
        if not unique_ids:
            return None
        timestamp = _now()

        def operation(connection: sqlite3.Connection) -> tuple[dict[str, Any] | None, str | None]:
            session = connection.execute(
                "SELECT workspace_id FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
            if session is None:
                return None, None
            placeholders = ",".join("?" for _ in unique_ids)
            rows = connection.execute(
                f"""
                SELECT id, sequence FROM messages
                WHERE session_id = ? AND id IN ({placeholders}) AND kind != 'skill'
                ORDER BY sequence ASC
                """,
                [session_id, *unique_ids],
            ).fetchall()
            if not rows:
                return None, session["workspace_id"]
            replacement_sequence = rows[0]["sequence"]
            selected = [row["id"] for row in rows]
            selected_placeholders = ",".join("?" for _ in selected)
            connection.execute(
                f"DELETE FROM messages WHERE id IN ({selected_placeholders})", selected
            )
            message_id = str(uuid.uuid4())
            summary_metadata = {
                "compacted_message_ids": selected,
                **dict(metadata or {}),
            }
            connection.execute(
                """
                INSERT INTO messages(
                    id, session_id, sequence, role, kind, name, content_json,
                    metadata_json, created_at
                ) VALUES(?, ?, ?, 'system', 'summary', 'conversation_summary', ?, ?, ?)
                """,
                (
                    message_id,
                    session_id,
                    replacement_sequence,
                    _json_dump(summary_content),
                    _json_dump(summary_metadata),
                    timestamp,
                ),
            )
            connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE id = ?", (timestamp, session_id)
            )
            row = connection.execute(
                "SELECT * FROM messages WHERE id = ?", (message_id,)
            ).fetchone()
            return _message_dict(row), session["workspace_id"]

        result, workspace_id = await self._write(operation)
        if result is not None and workspace_id:
            await self._invalidate_after_message(workspace_id, session_id)
        return result

    async def _invalidate_session(self, workspace_id: str, session_id: str) -> None:
        await self.cache.delete_prefix(f"session:")
        await self.cache.delete_prefix(f"sessions:{workspace_id}:")

    async def _invalidate_after_message(self, workspace_id: str, session_id: str) -> None:
        await self.cache.delete_prefix(f"messages:{session_id}:")
        await self._invalidate_session(workspace_id, session_id)


def _mapping_value(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _optional_string(value: Any) -> str | None:
    return str(value) if value is not None else None


__all__ = ["SQLiteStore"]
