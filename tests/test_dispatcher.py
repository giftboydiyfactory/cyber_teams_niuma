# tests/test_dispatcher.py
from __future__ import annotations

import json
import pytest

from niuma.dispatcher import Dispatcher, DispatchResult, build_dispatcher_prompt


def test_build_dispatcher_prompt_includes_sessions() -> None:
    sessions = [
        {"id": "a3f7", "status": "running", "prompt": "analyze code", "cwd": "/repo", "created_by": "jack@n.com"},
        {"id": "b2e1", "status": "completed", "prompt": "write tests", "cwd": "/repo2", "created_by": "alice@n.com"},
    ]
    prompt = build_dispatcher_prompt(
        user_prompt="how's the analysis going",
        user_email="jack@n.com",
        sessions=sessions,
    )
    assert "a3f7" in prompt
    assert "analyze code" in prompt
    assert "how's the analysis going" in prompt
    assert "jack@n.com" in prompt


def test_build_dispatcher_prompt_no_sessions() -> None:
    prompt = build_dispatcher_prompt(
        user_prompt="analyze this repo",
        user_email="jack@n.com",
        sessions=[],
    )
    assert "No known sessions" in prompt
    assert "analyze this repo" in prompt


def test_parse_dispatch_result_new() -> None:
    raw = json.dumps({
        "result": json.dumps({"action": "new", "prompt": "analyze code", "cwd": "/repo"}),
        "session_id": "ignored",
    })
    result = DispatchResult.from_claude_output(raw)
    assert result.action == "new"
    assert result.prompt == "analyze code"
    assert result.cwd == "/repo"


def test_parse_dispatch_result_resume() -> None:
    raw = json.dumps({
        "result": json.dumps({"action": "resume", "session_id": "a3f7", "prompt": "continue"}),
        "session_id": "ignored",
    })
    result = DispatchResult.from_claude_output(raw)
    assert result.action == "resume"
    assert result.session_id == "a3f7"


def test_parse_dispatch_result_reply() -> None:
    raw = json.dumps({
        "result": json.dumps({"action": "reply", "reply_text": "There are 2 sessions running."}),
        "session_id": "ignored",
    })
    result = DispatchResult.from_claude_output(raw)
    assert result.action == "reply"
    assert result.reply_text == "There are 2 sessions running."


def test_parse_dispatch_result_list() -> None:
    raw = json.dumps({
        "result": json.dumps({"action": "list"}),
        "session_id": "ignored",
    })
    result = DispatchResult.from_claude_output(raw)
    assert result.action == "list"
