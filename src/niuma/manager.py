# src/niuma/manager.py
"""Stateful Manager session — the 'team lead' that manages all worker sessions.

Unlike the old stateless Dispatcher, the Manager is a single persistent Claude Code
session that gets --resume'd for every user message. It remembers all context:
who asked what, which workers are running, what results came back.

The Manager returns structured JSON instructions that the bot executes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Optional

from niuma.config import ClaudeConfig
from niuma.session import _claude_command

if TYPE_CHECKING:
    from niuma.db import Database

logger = logging.getLogger(__name__)

_MANAGER_SYSTEM_PROMPT = """\
You are the niuma-bot Manager — the team lead of an AI worker team.

## Your Role
You receive messages from users via Teams chat. You decide how to handle each message:
- Answer ONLY trivially simple questions yourself (action: "reply")
- Delegate ALL other tasks to worker sessions (action: "new")
- Follow up with existing workers (action: "resume")

## CRITICAL: When to Use "reply" vs "new"

Use "reply" ONLY for responses you can give with 100% certainty from pure knowledge,
requiring NO tools, NO file access, NO shell commands, and NO system information:
- Pure math: "what is 2+2" → reply "4"
- Greetings: "hello" → reply "Hi!"
- Very simple factual questions: "what language is Python written in"

Use "new" (delegate to a worker) for EVERYTHING else, including:
- ANY request involving listing, scanning, searching, or querying
- "list sessions", "show history", "scan files", "check status" → ALWAYS use "new"
- ANY task that might benefit from shell commands, file access, or DB queries
- ANY task where you are not 100% certain of the answer from memory alone
- Anything involving the niuma DB, Claude session files, or system state

When in doubt, use "new". Workers are cheap; wrong answers are not.

## Your Capabilities
You have MEMORY — you remember all previous conversations, worker assignments, and results.
You manage a team of Claude Code worker sessions. Each worker has:
- A session ID (e.g. "0320-a7f3")
- A status (pending/running/completed/failed)
- A working directory
- A claude_session UUID for resuming

## Worker Infrastructure
Workers have access to:
- File system, shell commands, code editing
- teams-cli for Teams messages
- niuma DB at ~/.niuma/niuma.db
- Claude session history at ~/.claude/projects/

## Instructions Format
Return a JSON object with ONE of these actions:

1. {"action": "reply", "reply_text": "your answer"}
   ONLY for trivial questions (math, greetings) that need no tools whatsoever.

2. {"action": "new", "prompt": "task description", "cwd": "/path", "dedicated_chat": true/false}
   Delegate a new task. Set dedicated_chat=true for complex tasks with lots of output.

3. {"action": "resume", "session_id": "XXXX", "prompt": "follow-up instructions"}
   Send follow-up to an existing worker

4. {"action": "report", "reply_text": "status update"}
   Proactively report status/summary to the user

5. {"action": "new", "prompt": "...", "model": "opus"}
   Use "model" to override the default worker model for complex tasks.
   Options: "haiku" (simple/fast), "sonnet" (default), "opus" (complex reasoning)

## Guidelines
- When a user asks about worker status, check your memory first (you saw the results)
- When a worker finishes, you'll receive its output. Summarize and report to user.
- For complex requests, break them into subtasks and assign to multiple workers
- Keep the user informed about what's happening
- You are the SINGLE point of contact. Users don't talk to workers directly.
- When users ask about costs, report total and per-session costs from the DB

Return ONLY valid JSON. No other text.\
"""

_MANAGER_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "action": {"enum": ["new", "resume", "reply", "report"]},
        "session_id": {"type": "string"},
        "prompt": {"type": "string"},
        "cwd": {"type": "string"},
        "reply_text": {"type": "string"},
        "dedicated_chat": {"type": "boolean"},
        "model": {"type": "string"},
    },
    "required": ["action"],
})


@dataclass(frozen=True)
class ManagerDecision:
    action: str
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    cwd: Optional[str] = None
    reply_text: Optional[str] = None
    dedicated_chat: bool = False
    model: Optional[str] = None

    @classmethod
    def from_claude_output(cls, raw_output: str) -> "ManagerDecision":
        """Parse the JSON output from claude -p --output-format json."""
        outer = json.loads(raw_output)

        inner = outer.get("structured_output")
        if not isinstance(inner, dict):
            # Fallback: try parsing result as JSON
            result_str = outer.get("result", "")
            if isinstance(result_str, str) and result_str.strip().startswith("{"):
                try:
                    inner = json.loads(result_str)
                except json.JSONDecodeError:
                    inner = {}
            else:
                # Manager returned plain text — treat as reply
                inner = {"action": "reply", "reply_text": str(result_str or inner or "")}

        return cls(
            action=inner.get("action", "reply"),
            session_id=inner.get("session_id"),
            prompt=inner.get("prompt"),
            cwd=inner.get("cwd"),
            reply_text=inner.get("reply_text"),
            dedicated_chat=inner.get("dedicated_chat", False),
            model=inner.get("model"),
        )


_BOT_STATE_MANAGER_SESSION = "manager_session_id"


class Manager:
    """Stateful Manager that persists across interactions via --resume."""

    def __init__(self, config: ClaudeConfig, db: Optional["Database"] = None) -> None:
        self._config = config
        self._db = db
        self._session_id: Optional[str] = None
        self._initialized = False

    @property
    def session_id(self) -> Optional[str]:
        return self._session_id

    async def load_state(self) -> None:
        """Load persisted manager session_id from DB on startup."""
        if self._db is None:
            return
        try:
            saved = await self._db.get_bot_state(_BOT_STATE_MANAGER_SESSION)
            if saved:
                self._session_id = saved
                self._initialized = True
                logger.info("Resumed Manager session from DB: %s", saved[:12])
        except Exception:
            logger.warning("Could not load manager session from DB", exc_info=True)

    async def decide(
        self,
        *,
        user_message: str,
        user_email: str,
        context: str = "",
    ) -> ManagerDecision:
        """Send a message to the Manager and get a structured decision back.

        The Manager session is resumed each time, maintaining full conversation history.
        Context can include worker results, status updates, etc.
        """
        prompt = self._build_prompt(user_message, user_email, context)

        claude_cmd = _claude_command()
        if self._session_id:
            # Resume existing Manager session
            proc = await asyncio.create_subprocess_exec(
                *claude_cmd, "-p", prompt,
                "--resume", self._session_id,
                "--json-schema", _MANAGER_SCHEMA,
                "--output-format", "json",
                "--permission-mode", self._config.permission_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            # First call: create new Manager session
            proc = await asyncio.create_subprocess_exec(
                *claude_cmd, "-p", prompt,
                "--model", self._config.dispatcher_model,
                "--json-schema", _MANAGER_SCHEMA,
                "--system-prompt", _MANAGER_SYSTEM_PROMPT,
                "--output-format", "json",
                "--permission-mode", self._config.permission_mode,
                "-n", "niuma-manager",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Manager claude call failed (exit {proc.returncode}): "
                f"{stderr.decode().strip()[:200]}"
            )

        raw = stdout.decode()
        outer = json.loads(raw)

        # Save session ID for future resume (including across restarts)
        new_sid = outer.get("session_id")
        if new_sid and new_sid != self._session_id:
            self._session_id = new_sid
            self._initialized = True
            if self._db is not None:
                try:
                    await self._db.set_bot_state(_BOT_STATE_MANAGER_SESSION, new_sid)
                except Exception:
                    logger.warning("Failed to persist manager session_id to DB", exc_info=True)

        return ManagerDecision.from_claude_output(raw)

    async def feed_worker_result(
        self,
        *,
        session_id: str,
        result: str,
        status: str,
    ) -> ManagerDecision:
        """Feed a worker's result back to the Manager for processing.

        The Manager can then decide to report to user, assign follow-up, etc.
        Errors are caught and logged so callers (the watch loop) remain unaffected.
        """
        context = (
            f"[WORKER RESULT] Session [{session_id}] finished with status={status}.\n"
            f"Output:\n{result[:3000]}"
        )
        try:
            return await self.decide(
                user_message="",
                user_email="system",
                context=context,
            )
        except Exception:
            logger.exception(
                "feed_worker_result failed for session %s — returning no-op reply", session_id
            )
            return ManagerDecision(action="reply", reply_text="")

    def _build_prompt(
        self, user_message: str, user_email: str, context: str
    ) -> str:
        parts = []
        if context:
            parts.append(context)
        if user_message:
            parts.append(f"[USER MESSAGE from {user_email}]: {user_message}")
        return "\n\n".join(parts) if parts else "No new input."
