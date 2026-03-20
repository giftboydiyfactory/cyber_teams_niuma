# src/niuma/poller.py
from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Optional

from niuma.config import TeamsConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TeamsMessage:
    id: str
    sender: str
    sender_email: str
    body: str
    timestamp: str


class TeamsCliError(RuntimeError):
    """Error from teams-cli with exit code for specific handling."""

    def __init__(self, exit_code: int, message: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class Poller:
    def __init__(self, config: TeamsConfig) -> None:
        self._config = config

    async def poll_chat(self, chat_id: str, limit: int = 25) -> str:
        """Call teams-cli chat read and return raw JSON output."""
        proc = await asyncio.create_subprocess_exec(
            "teams-cli", "chat", "read", chat_id,
            "--limit", str(limit), "--json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            raise TeamsCliError(
                exit_code=proc.returncode,
                message=f"teams-cli chat read failed (exit {proc.returncode}): {error_msg}",
            )
        return stdout.decode()

    def parse_messages(self, raw_json: str) -> list[TeamsMessage]:
        """Parse teams-cli JSON output into TeamsMessage objects."""
        data = json.loads(raw_json)

        # Handle both formats: {"data": [...]} and {"success": true, "data": {"messages": [...]}}
        raw_data = data.get("data", [])
        if isinstance(raw_data, dict):
            messages_data = raw_data.get("messages", [])
        elif isinstance(raw_data, list):
            messages_data = raw_data
        else:
            logger.warning("Unexpected teams-cli data format")
            return []

        result = []
        for msg in messages_data:
            from_user = msg.get("from", {}).get("user", {})
            email = from_user.get("email", "")
            if not email:
                # Fallback: use displayName or user id when email not available
                email = from_user.get("displayName", from_user.get("id", "unknown"))
            body_raw = msg.get("body", {}).get("content", "")
            result.append(TeamsMessage(
                id=msg["id"],
                sender=from_user.get("displayName", "unknown"),
                sender_email=email,
                body=_strip_html(body_raw),
                timestamp=msg.get("createdDateTime", ""),
            ))
        return result

    def filter_triggered(self, messages: list[TeamsMessage]) -> list[TeamsMessage]:
        """Return only messages that start with the trigger prefix."""
        trigger = self._config.trigger.lower()
        return [m for m in messages if m.body.strip().lower().startswith(trigger)]

    def extract_prompt(self, message: TeamsMessage) -> str:
        """Strip trigger prefix from message body."""
        trigger = self._config.trigger
        body = message.body.strip()
        if body.lower().startswith(trigger.lower()):
            body = body[len(trigger):]
        return body.strip()

    def filter_new(
        self, messages: list[TeamsMessage], last_seen_id: Optional[str]
    ) -> list[TeamsMessage]:
        """Return messages newer than last_seen_id.

        Teams message IDs are numeric timestamps (milliseconds).
        Messages may be in any order from the API. We compare IDs
        numerically to determine which are newer.
        If last_seen_id is None, return all messages.
        """
        if last_seen_id is None:
            return messages

        try:
            last_seen_num = int(last_seen_id)
            return [m for m in messages if int(m.id) > last_seen_num]
        except ValueError:
            # Fallback for non-numeric IDs: positional search
            found = False
            new_messages = []
            for msg in messages:
                if found:
                    new_messages.append(msg)
                elif msg.id == last_seen_id:
                    found = True
            return new_messages


def _strip_html(text: str) -> str:
    """Remove HTML tags from text, preserving link URLs inline."""
    # Replace <a href="URL">text</a> with "text (URL)"
    text = re.sub(
        r'<a\s+[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
        r"\2 (\1)",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    # Replace <br/> and <br> with newline
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    # Remove remaining HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Decode HTML entities (handles &nbsp;, &amp;, &lt;, &gt;, and all others)
    text = html.unescape(text).replace("\xa0", " ")
    return text.strip()
