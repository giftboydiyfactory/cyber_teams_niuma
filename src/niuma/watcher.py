# src/niuma/watcher.py
"""Session watching logic for niuma-bot.

Extracted from main.py. Polls DB until a worker session finishes,
then sends the result to Teams and feeds it back to the Manager.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from niuma.main import NiumaBot

logger = logging.getLogger("niuma.watcher")

_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _get_session_progress(claude_session_id: str) -> Optional[str]:
    """Read the latest activity from a Claude session's JSONL file.

    Returns a short summary of what the worker is currently doing.
    """
    if not claude_session_id or not _CLAUDE_PROJECTS_DIR.exists():
        return None

    # Search all project dirs for the session JSONL
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        jsonl_path = project_dir / f"{claude_session_id}.jsonl"
        if jsonl_path.exists():
            return _parse_latest_activity(jsonl_path)

    return None


def _parse_latest_activity(jsonl_path: Path) -> Optional[str]:
    """Parse the last few entries from a session JSONL to get current activity."""
    try:
        lines = jsonl_path.read_text().strip().split("\n")
        # Read last 20 entries for context
        recent = lines[-20:] if len(lines) > 20 else lines

        last_tool = None
        last_assistant_text = None
        tool_count = 0

        for line in recent:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")

            if entry_type == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text = block.get("text", "").strip()
                                if text:
                                    last_assistant_text = text[:150]
                            elif block.get("type") == "tool_use":
                                last_tool = block.get("name", "unknown")
                                tool_count += 1

        parts = []
        if last_tool:
            parts.append(f"Using: {last_tool}")
        if last_assistant_text:
            # Take last line of text for brevity
            last_line = last_assistant_text.split("\n")[-1].strip()
            if last_line:
                parts.append(last_line[:100])

        if parts:
            return " | ".join(parts)
        return None

    except Exception:
        logger.debug("Failed to parse session JSONL", exc_info=True)
        return None


async def watch_session(
    bot: "NiumaBot",
    chat_id: str,
    session_id: str,
    reply_to: str = "",
) -> None:
    """Poll DB until session completes, then send result as thread reply.

    Also feeds the result back to the Manager so it can maintain context.
    """
    max_wait = bot._config.claude.session_timeout + 60
    elapsed = 0
    poll_interval = 2
    heartbeat_interval = 120  # Send progress update every 2 minutes
    last_heartbeat = 0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        session = await bot._session_mgr.get_result(session_id)
        if not session:
            return
        status = session["status"]

        # Send periodic heartbeat for long-running tasks
        if status == "running" and (elapsed - last_heartbeat) >= heartbeat_interval:
            last_heartbeat = elapsed
            minutes = elapsed // 60

            # Try to get actual progress from Claude session JSONL
            claude_sid = session.get("claude_session", "")
            progress = _get_session_progress(claude_sid)

            if progress:
                msg = f"⏳ session [{session_id}] ({minutes}m) — {progress}"
            else:
                msg = f"⏳ session [{session_id}] still working... ({minutes}m elapsed)"

            await bot._responder.send_text(chat_id, msg, reply_to=reply_to)

        if status in ("completed", "failed", "timeout"):
            result_text = session.get("last_output", "")
            error_text = None
            if status == "timeout":
                error_text = "Session timed out (24h)"
            elif status == "failed":
                error_text = result_text or "Unknown error"
                result_text = None

            # Send result to Teams
            if error_text:
                await bot._responder.send_result(
                    chat_id, session_id, error=error_text, reply_to=reply_to,
                )
            else:
                await bot._responder.send_result(
                    chat_id, session_id, result=result_text, reply_to=reply_to,
                )

            # Feed result back to Manager so it remembers — errors are caught internally
            mgr_decision = await bot._manager.feed_worker_result(
                session_id=session_id,
                result=result_text or error_text or "",
                status=status,
            )
            # If Manager wants to report something, send it
            if mgr_decision.action == "report" and mgr_decision.reply_text:
                await bot._responder.send_text(
                    chat_id, mgr_decision.reply_text, reply_to=reply_to,
                )

            return
