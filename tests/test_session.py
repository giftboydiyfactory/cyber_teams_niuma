# tests/test_session.py
from __future__ import annotations

import asyncio
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

from niuma.session import SessionManager
from niuma.db import Database


@pytest.fixture
async def db(tmp_dir: Path) -> Database:
    database = Database(str(tmp_dir / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.fixture
def session_mgr(db: Database, sample_config: dict) -> SessionManager:
    from niuma.config import ClaudeConfig

    claude_cfg = ClaudeConfig(
        dispatcher_model="sonnet",
        worker_model="sonnet",
        max_concurrent=2,
        session_timeout=10,
        permission_mode="auto",
        default_cwd="/tmp",
    )
    return SessionManager(claude_cfg, db)


def _mock_claude_success(result_text: str = "Done", session_id: str = "uuid-abc") -> AsyncMock:
    output = json.dumps({
        "result": result_text,
        "session_id": session_id,
        "total_cost_usd": 0.05,
    })
    mock_proc = AsyncMock()
    mock_proc.communicate = AsyncMock(return_value=(output.encode(), b""))
    mock_proc.returncode = 0
    mock_proc.pid = 12345
    mock_proc.kill = MagicMock()
    return mock_proc


@pytest.mark.asyncio
async def test_start_session(session_mgr: SessionManager, db: Database) -> None:
    mock_proc = _mock_claude_success("Analysis complete", "uuid-123")

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
        session = await session_mgr.start_session(
            chat_id="chat-1",
            created_by="user@nvidia.com",
            prompt="analyze code",
            cwd="/tmp/repo",
        )

    assert session["status"] == "pending"
    await asyncio.sleep(0.1)
    updated = await db.get_session(session["id"])
    assert updated["status"] == "completed"
    assert updated["claude_session"] == "uuid-123"


@pytest.mark.asyncio
async def test_max_concurrent_enforced(session_mgr: SessionManager) -> None:
    never_finish = AsyncMock()
    block_event = asyncio.Event()

    async def _block_forever():
        await block_event.wait()
        return b"", b""

    never_finish.communicate = _block_forever
    never_finish.returncode = None
    never_finish.pid = 99999
    never_finish.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=never_finish):
        await session_mgr.start_session(
            chat_id="c", created_by="u", prompt="p1", cwd="/tmp"
        )
        await session_mgr.start_session(
            chat_id="c", created_by="u", prompt="p2", cwd="/tmp"
        )

    assert session_mgr.active_count == 2

    with pytest.raises(RuntimeError, match="concurrent"):
        await session_mgr.start_session(
            chat_id="c", created_by="u", prompt="p3", cwd="/tmp"
        )

    block_event.set()  # unblock so tasks can finish cleanly


@pytest.mark.asyncio
async def test_stop_session(session_mgr: SessionManager, db: Database) -> None:
    never_finish = AsyncMock()
    future = asyncio.get_event_loop().create_future()
    never_finish.communicate = AsyncMock(side_effect=lambda: future)
    never_finish.returncode = None
    never_finish.pid = 99999
    never_finish.kill = MagicMock()

    with patch("asyncio.create_subprocess_exec", return_value=never_finish):
        session = await session_mgr.start_session(
            chat_id="c", created_by="u", prompt="p", cwd="/tmp"
        )

    await session_mgr.stop_session(session["id"])
    never_finish.kill.assert_called_once()
