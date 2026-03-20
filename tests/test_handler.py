# tests/test_handler.py
"""Tests for handler.py role-based permissions."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock

from niuma.handler import get_user_role, handle_message, _MEMBER_PERMISSION_DENIED, _MEMBER_PERMISSION_HINT
from niuma.manager import ManagerDecision


# ---------------------------------------------------------------------------
# get_user_role unit tests
# ---------------------------------------------------------------------------

def test_get_user_role_admin() -> None:
    role = get_user_role(
        "admin@example.com",
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
    )
    assert role == "admin"


def test_get_user_role_member() -> None:
    role = get_user_role(
        "member@example.com",
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
    )
    assert role == "member"


def test_get_user_role_unknown() -> None:
    role = get_user_role(
        "stranger@example.com",
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
    )
    assert role == "unknown"


def test_get_user_role_admin_takes_precedence_over_allowed() -> None:
    """If a user is in both lists, admin wins."""
    role = get_user_role(
        "both@example.com",
        admin_users=["both@example.com"],
        allowed_users=["both@example.com"],
    )
    assert role == "admin"


def test_get_user_role_empty_lists() -> None:
    role = get_user_role(
        "anyone@example.com",
        admin_users=[],
        allowed_users=[],
    )
    assert role == "unknown"


# ---------------------------------------------------------------------------
# handle_message integration tests (mocked bot)
# ---------------------------------------------------------------------------


def _make_bot(
    admin_users: list[str],
    allowed_users: list[str],
    manager_decision: ManagerDecision,
    reply_only: bool = False,
) -> MagicMock:
    """Create a minimal mock NiumaBot for handle_message tests."""
    bot = MagicMock()

    # Config
    bot._config.security.admin_users = admin_users
    bot._config.security.allowed_users = allowed_users

    # reply-only mode
    bot._is_reply_only.return_value = reply_only

    # Manager
    bot._manager.decide = AsyncMock(return_value=manager_decision)

    # Responder
    bot._responder.send_text = AsyncMock()

    return bot


@pytest.mark.asyncio
async def test_admin_can_start_new_session() -> None:
    """Admin users should be able to trigger action='new'."""
    decision = ManagerDecision(action="new", prompt="run tests", cwd="/repo")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=[],
        manager_decision=decision,
    )
    bot._session_mgr.start_session = AsyncMock(return_value={"id": "sess-001"})
    bot._responder.send_processing = AsyncMock()
    bot._fire_and_track = MagicMock()

    await handle_message(bot, "chat-1", "admin@example.com", "run tests", "msg-1")

    # Manager.decide should have been called without the member hint
    call_kwargs = bot._manager.decide.call_args.kwargs
    assert _MEMBER_PERMISSION_HINT not in call_kwargs["user_message"]

    # session_mgr.start_session should have been invoked (action='new' allowed)
    bot._session_mgr.start_session.assert_called_once()

    # Permission denied message should NOT have been sent
    for call in bot._responder.send_text.call_args_list:
        assert _MEMBER_PERMISSION_DENIED not in (call.args[1] if call.args else "")


@pytest.mark.asyncio
async def test_member_new_action_blocked() -> None:
    """Members should be blocked from action='new' with a permission-denied message."""
    decision = ManagerDecision(action="new", prompt="run tests", cwd="/repo")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "member@example.com", "run tests", "msg-1")

    # Manager.decide should have been called WITH the member hint
    call_kwargs = bot._manager.decide.call_args.kwargs
    assert _MEMBER_PERMISSION_HINT in call_kwargs["user_message"]

    # Permission-denied reply must be sent
    bot._responder.send_text.assert_called_once_with(
        "chat-1", _MEMBER_PERMISSION_DENIED, reply_to="msg-1"
    )


@pytest.mark.asyncio
async def test_member_resume_action_blocked() -> None:
    """Members should be blocked from action='resume'."""
    decision = ManagerDecision(action="resume", session_id="sess-123", prompt="continue")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "member@example.com", "continue session", "msg-2")

    bot._responder.send_text.assert_called_once_with(
        "chat-1", _MEMBER_PERMISSION_DENIED, reply_to="msg-2"
    )


@pytest.mark.asyncio
async def test_member_reply_action_allowed() -> None:
    """Members should be able to receive reply-action responses."""
    decision = ManagerDecision(action="reply", reply_text="Hello there!")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "member@example.com", "hello", "msg-3")

    bot._responder.send_text.assert_called_once_with("chat-1", "Hello there!", reply_to="msg-3")


@pytest.mark.asyncio
async def test_member_report_action_allowed() -> None:
    """Members can receive report-action responses."""
    decision = ManagerDecision(action="report", reply_text="Status: all good.")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "member@example.com", "status?", "msg-4")

    bot._responder.send_text.assert_called_once_with(
        "chat-1", "Status: all good.", reply_to="msg-4"
    )


@pytest.mark.asyncio
async def test_member_hint_appended_to_prompt() -> None:
    """For members, the system hint must be appended to the prompt sent to Manager."""
    decision = ManagerDecision(action="reply", reply_text="Sure!")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=["member@example.com"],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "member@example.com", "can you help?", "msg-5")

    call_kwargs = bot._manager.decide.call_args.kwargs
    assert "can you help?" in call_kwargs["user_message"]
    assert _MEMBER_PERMISSION_HINT in call_kwargs["user_message"]


@pytest.mark.asyncio
async def test_admin_hint_not_appended_to_prompt() -> None:
    """For admins, the system hint must NOT be added to the prompt."""
    decision = ManagerDecision(action="reply", reply_text="Done.")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=[],
        manager_decision=decision,
    )

    await handle_message(bot, "chat-1", "admin@example.com", "do something", "msg-6")

    call_kwargs = bot._manager.decide.call_args.kwargs
    assert _MEMBER_PERMISSION_HINT not in call_kwargs["user_message"]


@pytest.mark.asyncio
async def test_reply_only_chat_still_enforced_for_admin() -> None:
    """Even admins are subject to reply-only chat mode for non-reply/report actions."""
    decision = ManagerDecision(action="new", prompt="task", reply_text="Reply text")
    bot = _make_bot(
        admin_users=["admin@example.com"],
        allowed_users=[],
        manager_decision=decision,
        reply_only=True,
    )
    bot._session_mgr = MagicMock()

    await handle_message(bot, "reply-chat", "admin@example.com", "do something", "msg-7")

    # Should send reply-only message, not start a new session
    bot._responder.send_text.assert_called_once()
    args = bot._responder.send_text.call_args.args
    assert args[0] == "reply-chat"


@pytest.mark.asyncio
async def test_manager_failure_is_handled_gracefully() -> None:
    """If Manager.decide raises, handle_message should log and return silently."""
    bot = MagicMock()
    bot._config.security.admin_users = ["admin@example.com"]
    bot._config.security.allowed_users = []
    bot._is_reply_only.return_value = False
    bot._manager.decide = AsyncMock(side_effect=RuntimeError("claude crashed"))
    bot._responder.send_text = AsyncMock()

    # Should not raise
    await handle_message(bot, "chat-1", "admin@example.com", "hello", "msg-8")

    # No reply should be sent when manager fails
    bot._responder.send_text.assert_not_called()
