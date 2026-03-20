from __future__ import annotations

import pytest
from pathlib import Path

from niuma.db import Database


@pytest.fixture
async def db(tmp_dir: Path) -> Database:
    database = Database(str(tmp_dir / "test.db"))
    await database.init()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_init_creates_tables(db: Database) -> None:
    tables = await db.list_tables()
    assert "sessions" in tables
    assert "messages" in tables
    assert "poll_state" in tables


@pytest.mark.asyncio
async def test_create_and_get_session(db: Database) -> None:
    session = await db.create_session(
        chat_id="chat-1",
        created_by="user@nvidia.com",
        prompt="test prompt",
        cwd="/tmp",
        model="sonnet",
    )
    assert len(session["id"]) == 7  # format: MMDD-XX (e.g. "0320-a7")
    assert session["status"] == "pending"

    fetched = await db.get_session(session["id"])
    assert fetched is not None
    assert fetched["prompt"] == "test prompt"


@pytest.mark.asyncio
async def test_update_session_status(db: Database) -> None:
    session = await db.create_session(
        chat_id="chat-1",
        created_by="user@nvidia.com",
        prompt="test",
        cwd="/tmp",
        model="sonnet",
    )
    await db.update_session(session["id"], status="running", claude_session="uuid-123")
    updated = await db.get_session(session["id"])
    assert updated["status"] == "running"
    assert updated["claude_session"] == "uuid-123"


@pytest.mark.asyncio
async def test_list_sessions_by_status(db: Database) -> None:
    await db.create_session(chat_id="c", created_by="u", prompt="p1", cwd="/", model="s")
    s2 = await db.create_session(chat_id="c", created_by="u", prompt="p2", cwd="/", model="s")
    await db.update_session(s2["id"], status="running")

    running = await db.list_sessions(status="running")
    assert len(running) == 1
    assert running[0]["prompt"] == "p2"


@pytest.mark.asyncio
async def test_poll_state(db: Database) -> None:
    await db.set_poll_state("chat-1", "msg-100")
    state = await db.get_poll_state("chat-1")
    assert state == "msg-100"

    await db.set_poll_state("chat-1", "msg-200")
    state = await db.get_poll_state("chat-1")
    assert state == "msg-200"


@pytest.mark.asyncio
async def test_add_and_get_messages(db: Database) -> None:
    session = await db.create_session(
        chat_id="c", created_by="u", prompt="p", cwd="/", model="s"
    )
    await db.add_message(session["id"], "user", "hello")
    await db.add_message(session["id"], "assistant", "hi there")

    msgs = await db.get_messages(session["id"])
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
