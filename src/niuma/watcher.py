# src/niuma/watcher.py
"""Session watching logic for niuma-bot.

Extracted from main.py. Polls DB until a worker session finishes,
then sends the result to Teams and feeds it back to the Manager.
"""
from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from niuma.main import NiumaBot

logger = logging.getLogger("niuma.watcher")


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
            await bot._responder.send_text(
                chat_id,
                f"⏳ session [{session_id}] still working... ({minutes}m elapsed)",
                reply_to=reply_to,
            )

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
