# src/niuma/db.py
from __future__ import annotations

import secrets
import time
from pathlib import Path
from typing import Any, Optional

import aiosqlite

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id              TEXT PRIMARY KEY,
    claude_session  TEXT,
    chat_id         TEXT NOT NULL,
    created_by      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    cwd             TEXT,
    model           TEXT,
    prompt          TEXT,
    last_output     TEXT,
    cost_usd        REAL DEFAULT 0,
    trigger_message_id TEXT,
    created_at      REAL NOT NULL,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    role            TEXT NOT NULL,
    content         TEXT,
    created_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS poll_state (
    chat_id         TEXT PRIMARY KEY,
    last_message_id TEXT,
    updated_at      REAL NOT NULL
);
"""


def _short_id() -> str:
    return secrets.token_hex(2)


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None

    async def init(self) -> None:
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(_SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def list_tables(self) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [r["name"] for r in rows]

    async def create_session(
        self,
        *,
        chat_id: str,
        created_by: str,
        prompt: str,
        cwd: str,
        model: str,
        trigger_message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        now = time.time()
        sid = _short_id()
        await self._conn.execute(
            """INSERT INTO sessions (id, chat_id, created_by, status, prompt, cwd, model, trigger_message_id, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (sid, chat_id, created_by, prompt, cwd, model, trigger_message_id, now, now),
        )
        await self._conn.commit()
        return dict(await self._get_row("sessions", sid))

    async def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        row = await self._get_row("sessions", session_id)
        return dict(row) if row else None

    async def update_session(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [session_id]
        await self._conn.execute(
            f"UPDATE sessions SET {sets} WHERE id = ?", vals
        )
        await self._conn.commit()

    async def list_sessions(
        self, *, status: Optional[str] = None, created_by: Optional[str] = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM sessions WHERE 1=1"
        params: list[Any] = []
        if status:
            query += " AND status = ?"
            params.append(status)
        if created_by:
            query += " AND created_by = ?"
            params.append(created_by)
        query += " ORDER BY created_at DESC"
        cursor = await self._conn.execute(query, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def set_poll_state(self, chat_id: str, last_message_id: str) -> None:
        now = time.time()
        await self._conn.execute(
            """INSERT INTO poll_state (chat_id, last_message_id, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET last_message_id=?, updated_at=?""",
            (chat_id, last_message_id, now, last_message_id, now),
        )
        await self._conn.commit()

    async def get_poll_state(self, chat_id: str) -> Optional[str]:
        cursor = await self._conn.execute(
            "SELECT last_message_id FROM poll_state WHERE chat_id = ?", (chat_id,)
        )
        row = await cursor.fetchone()
        return row["last_message_id"] if row else None

    async def add_message(
        self, session_id: str, role: str, content: str
    ) -> None:
        now = time.time()
        await self._conn.execute(
            """INSERT INTO messages (session_id, role, content, created_at)
               VALUES (?, ?, ?, ?)""",
            (session_id, role, content, now),
        )
        await self._conn.commit()

    async def get_messages(self, session_id: str) -> list[dict[str, Any]]:
        cursor = await self._conn.execute(
            "SELECT * FROM messages WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def get_session_by_claude_id(self, claude_session: str) -> Optional[dict[str, Any]]:
        """Find a session by its claude session UUID (or prefix)."""
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE claude_session LIKE ?",
            (claude_session + "%",),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def import_session(
        self,
        *,
        claude_session: str,
        chat_id: str,
        created_by: str,
        prompt: str,
        cwd: str,
        status: str = "completed",
    ) -> dict[str, Any]:
        """Import an external Claude session into niuma DB."""
        # Check if already imported
        existing = await self.get_session_by_claude_id(claude_session)
        if existing:
            return existing

        now = time.time()
        sid = _short_id()
        await self._conn.execute(
            """INSERT INTO sessions (id, claude_session, chat_id, created_by, status, prompt, cwd, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)""",
            (sid, claude_session, chat_id, created_by, status, prompt, cwd, now, now),
        )
        await self._conn.commit()
        return dict(await self._get_row("sessions", sid))

    async def _get_row(self, table: str, row_id: str) -> Optional[aiosqlite.Row]:
        cursor = await self._conn.execute(
            f"SELECT * FROM {table} WHERE id = ?", (row_id,)
        )
        return await cursor.fetchone()
