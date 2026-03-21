# tests/test_responder.py
from __future__ import annotations

import pytest

from niuma.responder import format_result, format_status, format_session_list, format_processing


def test_format_result_short() -> None:
    html = format_result(session_id="a3f7", result="Analysis complete.")
    assert "a3f7" in html
    assert "Analysis complete." in html
    assert "niuma-bot" in html


def test_format_result_truncated() -> None:
    long_result = "x" * 3000
    html = format_result(session_id="a3f7", result=long_result)
    assert "truncated" in html.lower() or "output saved" in html.lower()
    assert len(html) < 3000


def test_format_result_error() -> None:
    html = format_result(session_id="a3f7", result=None, error="Claude crashed")
    assert "a3f7" in html
    assert "Claude crashed" in html


def test_format_status_running() -> None:
    session = {"id": "a3f7", "status": "running", "prompt": "analyze code", "created_by": "j@n.com"}
    html = format_status(session)
    assert "running" in html.lower()
    assert "a3f7" in html


def test_format_session_list() -> None:
    sessions = [
        {"id": "a3f7", "status": "completed", "prompt": "analyze code", "created_by": "jack@n.com"},
        {"id": "b2e1", "status": "running", "prompt": "write tests", "created_by": "alice@n.com"},
    ]
    html = format_session_list(sessions)
    assert "a3f7" in html
    assert "b2e1" in html
    assert "jack@n.com" in html


def test_format_processing() -> None:
    html = format_processing(session_id="a3f7")
    assert "a3f7" in html
