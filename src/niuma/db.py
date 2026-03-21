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
    session_chat_id TEXT,
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

CREATE TABLE IF NOT EXISTS bot_state (
    key             TEXT PRIMARY KEY,
    value           TEXT,
    updated_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS watched_chats (
    chat_id         TEXT PRIMARY KEY,
    mode            TEXT NOT NULL DEFAULT 'full',
    added_by        TEXT NOT NULL,
    added_at        REAL NOT NULL
);
"""


_ALLOWED_SESSION_FIELDS = frozenset({
    "claude_session",
    "chat_id",
    "created_by",
    "status",
    "cwd",
    "model",
    "prompt",
    "last_output",
    "cost_usd",
    "trigger_message_id",
    "session_chat_id",
    "updated_at",
})


def _short_id() -> str:
    """Generate a short, time-sortable session ID.

    Format: MMDD-XXXX (date prefix + 4 random hex chars)
    Examples: 0320-a7f3, 0320-f3c1, 0321-0b9e
    - Date prefix makes IDs naturally sortable and human-readable
    - Random suffix avoids collisions within the same day (65536 possibilities)
    - Total: ~24M unique IDs per year
    """
    import datetime
    now = datetime.datetime.now()
    date_prefix = now.strftime("%m%d")
    random_suffix = secrets.token_hex(2)
    return f"{date_prefix}-{random_suffix}"


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
        await self._migrate()

    async def _migrate(self) -> None:
        """Apply any additive migrations that are safe to re-run.

        Uses ALTER TABLE … ADD COLUMN IF NOT EXISTS idiom via a try/except
        so that this method is idempotent on both fresh and existing DBs.
        """
        migrations = [
            # Add bot_state table (already in _SCHEMA but guard for very old DBs)
            "CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT, updated_at REAL NOT NULL)",
            # Add watched_chats table
            "CREATE TABLE IF NOT EXISTS watched_chats (chat_id TEXT PRIMARY KEY, mode TEXT NOT NULL DEFAULT 'full', added_by TEXT NOT NULL, added_at REAL NOT NULL)",
        ]
        for stmt in migrations:
            try:
                await self._conn.execute(stmt)
            except Exception:
                pass  # already applied
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
        # Retry on collision (up to 10 attempts)
        for _ in range(10):
            sid = _short_id()
            existing = await self._get_row(sid)
            if not existing:
                break
        else:
            raise RuntimeError("Failed to generate a unique session ID after 10 attempts")
        await self._conn.execute(
            """INSERT INTO sessions (id, chat_id, created_by, status, prompt, cwd, model, trigger_message_id, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (sid, chat_id, created_by, prompt, cwd, model, trigger_message_id, now, now),
        )
        await self._conn.commit()
        return dict(await self._get_row(sid))

    async def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        """Find session by short ID, claude session UUID, or prefix of either."""
        # Exact match on short ID
        row = await self._get_row(session_id)
        if row:
            return dict(row)

        # Escape LIKE wildcards in user-supplied input
        escaped = session_id.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

        # Prefix match on short ID
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE id LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 1",
            (escaped + "%",),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)

        # Prefix match on claude_session UUID
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE claude_session LIKE ? ESCAPE '\\' ORDER BY created_at DESC LIMIT 1",
            ("%" + escaped + "%",),
        )
        row = await cursor.fetchone()
        if row:
            return dict(row)

        return None

    async def update_session(self, session_id: str, **fields: Any) -> None:
        fields["updated_at"] = time.time()
        invalid = set(fields) - _ALLOWED_SESSION_FIELDS
        if invalid:
            raise ValueError(f"Invalid session fields: {invalid}")
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

    async def get_queued_messages(self, session_id: str) -> list[str]:
        """Get user messages that were queued while the worker was busy.

        Returns messages added after the last assistant message, then deletes them
        so they aren't sent twice.
        """
        # Find the timestamp of the last assistant message
        cursor = await self._conn.execute(
            "SELECT MAX(created_at) FROM messages WHERE session_id = ? AND role = 'assistant'",
            (session_id,),
        )
        row = await cursor.fetchone()
        last_assistant_ts = row[0] if row and row[0] else 0

        # Get user messages after that
        cursor = await self._conn.execute(
            "SELECT content FROM messages WHERE session_id = ? AND role = 'user' AND created_at > ? ORDER BY created_at",
            (session_id, last_assistant_ts),
        )
        rows = await cursor.fetchall()
        messages = [r[0] for r in rows if r[0].strip()]

        # Delete the queued messages so they aren't sent again
        if messages:
            await self._conn.execute(
                "DELETE FROM messages WHERE session_id = ? AND role = 'user' AND created_at > ?",
                (session_id, last_assistant_ts),
            )
            await self._conn.commit()

        return messages

    async def get_session_by_chat_id(self, session_chat_id: str) -> Optional[dict[str, Any]]:
        """Find the most recent session bound to a dedicated chat."""
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE session_chat_id = ? ORDER BY created_at DESC LIMIT 1",
            (session_chat_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def list_session_chat_ids(self) -> list[str]:
        """Return all active session_chat_ids for polling."""
        cursor = await self._conn.execute(
            "SELECT DISTINCT session_chat_id FROM sessions WHERE session_chat_id IS NOT NULL"
        )
        rows = await cursor.fetchall()
        return [r["session_chat_id"] for r in rows]

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
        for _ in range(10):
            sid = _short_id()
            existing = await self._get_row(sid)
            if not existing:
                break
        else:
            raise RuntimeError("Failed to generate a unique session ID after 10 attempts")
        await self._conn.execute(
            """INSERT INTO sessions (id, claude_session, chat_id, created_by, status, prompt, cwd, model, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'unknown', ?, ?)""",
            (sid, claude_session, chat_id, created_by, status, prompt, cwd, now, now),
        )
        await self._conn.commit()
        return dict(await self._get_row(sid))

    async def get_bot_state(self, key: str) -> Optional[str]:
        """Retrieve a bot-level state value by key."""
        cursor = await self._conn.execute(
            "SELECT value FROM bot_state WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def set_bot_state(self, key: str, value: str) -> None:
        """Persist a bot-level state value."""
        now = time.time()
        await self._conn.execute(
            """INSERT INTO bot_state (key, value, updated_at)
               VALUES (?, ?, ?)
               ON CONFLICT(key) DO UPDATE SET value=?, updated_at=?""",
            (key, value, now, value, now),
        )
        await self._conn.commit()

    async def get_total_cost_usd(self) -> float:
        """Return the sum of cost_usd across all sessions."""
        cursor = await self._conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM sessions"
        )
        row = await cursor.fetchone()
        return float(row["total"]) if row else 0.0

    async def get_total_cost(self) -> float:
        """Get total cost across all sessions."""
        async with self._conn.execute("SELECT COALESCE(SUM(cost_usd), 0) FROM sessions") as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0.0

    async def get_session_costs(self, limit: int = 10) -> list[dict]:
        """Get recent sessions with their costs."""
        async with self._conn.execute(
            "SELECT id, prompt, cost_usd, status, created_at FROM sessions WHERE cost_usd > 0 ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()
            return [{"id": r[0], "prompt": r[1][:50], "cost": r[2], "status": r[3]} for r in rows]

    async def add_watched_chat(self, chat_id: str, added_by: str, mode: str = "full") -> None:
        """Add a chat to the watched_chats table (or update its mode if already present)."""
        now = time.time()
        await self._conn.execute(
            """INSERT INTO watched_chats (chat_id, mode, added_by, added_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET mode=?, added_by=?, added_at=?""",
            (chat_id, mode, added_by, now, mode, added_by, now),
        )
        await self._conn.commit()

    async def remove_watched_chat(self, chat_id: str) -> None:
        """Remove a chat from the watched_chats table."""
        await self._conn.execute(
            "DELETE FROM watched_chats WHERE chat_id = ?", (chat_id,)
        )
        await self._conn.commit()

    async def list_watched_chats(self) -> list[dict[str, Any]]:
        """Return all watched chats."""
        cursor = await self._conn.execute(
            "SELECT chat_id, mode, added_by, added_at FROM watched_chats ORDER BY added_at"
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def cleanup_expired_sessions(self, max_age_seconds: int = 86400 * 7) -> int:
        """Mark sessions older than max_age as expired. Returns count of expired sessions."""
        cutoff = time.time() - max_age_seconds
        async with self._conn.execute(
            "UPDATE sessions SET status = 'expired' WHERE status IN ('completed', 'failed') AND created_at < ?",
            (cutoff,),
        ) as cursor:
            count = cursor.rowcount
        await self._conn.commit()
        return count

    async def _get_row(self, row_id: str) -> Optional[aiosqlite.Row]:
        cursor = await self._conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (row_id,)
        )
        return await cursor.fetchone()
