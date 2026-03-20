# src/niuma/responder.py
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Optional

import markdown

logger = logging.getLogger(__name__)

_SIGNATURE = "<hr/><p><em>Sent via Claude Code (ai-pim-utils)</em></p>"
_MAX_BODY_LEN = 2000


def format_processing(session_id: str) -> str:
    return f"<p>session [<b>{session_id}</b>] processing...</p>{_SIGNATURE}"


def format_result(
    session_id: str,
    result: Optional[str] = None,
    error: Optional[str] = None,
    output_dir: Optional[str] = None,
) -> str:
    if error:
        return (
            f"<p>session [<b>{session_id}</b>] failed</p>"
            f"<p><code>{_escape(error[:500])}</code></p>"
            f"{_SIGNATURE}"
        )

    text = result or ""
    if len(text) <= _MAX_BODY_LEN:
        body_html = _md_to_html(text)
        return (
            f"<p><b>session [{session_id}] done</b></p>"
            f"{body_html}"
            f"{_SIGNATURE}"
        )

    # Save full output, send truncated
    saved_path = ""
    if output_dir:
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{session_id}.md"
        out_file.write_text(text)
        saved_path = str(out_file)

    summary = text[:_MAX_BODY_LEN]
    body_html = _md_to_html(summary)
    truncated_note = (
        f" Full output saved to <code>{saved_path}</code>"
        if saved_path else " (output truncated)"
    )

    return (
        f"<p><b>session [{session_id}] done</b></p>"
        f"{body_html}"
        f"<p><em>...{truncated_note}</em></p>"
        f"{_SIGNATURE}"
    )


def format_status(session: dict[str, Any]) -> str:
    status_icon = {
        "pending": "pending", "running": "running", "completed": "completed",
        "failed": "failed", "timeout": "timeout",
    }.get(session["status"], "unknown")

    return (
        f"<p>{status_icon} session [<b>{session['id']}</b>] - "
        f"{session['status']}</p>"
        f"<p>By: {session['created_by']}<br/>"
        f"Prompt: {_escape(session['prompt'][:100])}</p>"
        f"{_SIGNATURE}"
    )


def format_session_list(sessions: list[dict[str, Any]]) -> str:
    if not sessions:
        return f"<p>No sessions.</p>{_SIGNATURE}"

    items = []
    for s in sessions:
        items.append(
            f"<li>[<b>{s['id']}</b>] {s['status']} - "
            f"{s['created_by']} - {_escape(s['prompt'][:60])}</li>"
        )

    return (
        f"<p>Sessions:</p>"
        f"<ul>{''.join(items)}</ul>"
        f"{_SIGNATURE}"
    )


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _md_to_html(text: str) -> str:
    """Convert Markdown text to HTML for Teams rendering."""
    return markdown.markdown(
        text,
        extensions=["tables", "fenced_code", "nl2br"],
    )


class Responder:
    def __init__(self, output_dir: str = "~/.niuma/outputs") -> None:
        self._output_dir = str(Path(output_dir).expanduser())

    async def send(
        self, chat_id: str, html_body: str,
        reply_to: Optional[str] = None,
    ) -> None:
        """Send an HTML message to a Teams chat.

        Note: Teams chat API does not support thread replies (only channel messages do).
        The reply_to parameter is accepted but currently unused — kept for future
        channel support.
        """
        env = {**os.environ, "READ_WRITE_MODE": "1"}
        proc = await asyncio.create_subprocess_exec(
            "teams-cli", "chat", "send", chat_id,
            "--html", "--body", html_body,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(
                "Failed to send Teams message (exit %d): %s",
                proc.returncode, stderr.decode().strip()[:200],
            )
            raise RuntimeError(f"teams-cli send failed: {stderr.decode().strip()}")

    async def send_processing(
        self, chat_id: str, session_id: str,
        reply_to: Optional[str] = None,
    ) -> None:
        await self.send(chat_id, format_processing(session_id), reply_to=reply_to)

    async def send_result(
        self, chat_id: str, session_id: str,
        result: Optional[str] = None, error: Optional[str] = None,
        reply_to: Optional[str] = None,
    ) -> None:
        html = format_result(session_id, result, error, self._output_dir)
        await self.send(chat_id, html, reply_to=reply_to)

    async def send_status(
        self, chat_id: str, session: dict[str, Any],
        reply_to: Optional[str] = None,
    ) -> None:
        await self.send(chat_id, format_status(session), reply_to=reply_to)

    async def send_session_list(
        self, chat_id: str, sessions: list[dict[str, Any]],
        reply_to: Optional[str] = None,
    ) -> None:
        await self.send(chat_id, format_session_list(sessions), reply_to=reply_to)

    async def send_text(
        self, chat_id: str, text: str,
        reply_to: Optional[str] = None,
    ) -> None:
        body_html = _md_to_html(text)
        html = f"{body_html}{_SIGNATURE}"
        await self.send(chat_id, html, reply_to=reply_to)
