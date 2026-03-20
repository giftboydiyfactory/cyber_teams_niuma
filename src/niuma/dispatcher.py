# src/niuma/dispatcher.py
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

from niuma.config import ClaudeConfig

logger = logging.getLogger(__name__)

_DISPATCHER_SYSTEM_PROMPT = """\
You are the niuma-bot dispatcher. Your job is to route user messages to the right action.

You receive:
- The user's message
- The user's email
- A list of currently tracked sessions (id, status, prompt summary, cwd, created_by)

You must decide:
1. Is the user referring to an existing session? (e.g., "how's that going", "continue the analysis")
   -> Return action "resume" with the session_id and the user's follow-up prompt.
2. Is the user requesting a new task?
   -> Return action "new" with the prompt and inferred cwd (from paths mentioned, or null).
3. Is the user asking a simple question that doesn't need a worker session?
   -> Return action "reply" with reply_text containing your direct answer.
4. Is the user asking about session status?
   -> Return action "status" with session_id if specific, or "list" if asking about all.
5. Does the user want to stop a session?
   -> Return action "stop" with the session_id.

Return ONLY valid JSON matching the required schema. No other text.\
"""

_DISPATCH_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "action": {"enum": ["new", "resume", "reply", "status", "stop", "list"]},
        "session_id": {"type": "string"},
        "prompt": {"type": "string"},
        "cwd": {"type": "string"},
        "reply_text": {"type": "string"},
        "model": {"type": "string"},
    },
    "required": ["action"],
})


def build_dispatcher_prompt(
    *,
    user_prompt: str,
    user_email: str,
    sessions: list[dict[str, Any]],
) -> str:
    """Build the prompt sent to the dispatcher Claude session."""
    if sessions:
        session_lines = []
        for s in sessions:
            last_output_preview = ""
            if s.get("last_output"):
                last_output_preview = f' last_result="{s["last_output"][:120]}..."'
            session_lines.append(
                f"  - [{s['id']}] status={s['status']} by={s['created_by']} "
                f"cwd={s.get('cwd', 'N/A')} prompt=\"{s['prompt'][:120]}\""
                f"{last_output_preview}"
            )
        sessions_text = "Known sessions:\n" + "\n".join(session_lines)
    else:
        sessions_text = "No known sessions."

    return f"""\
User: {user_email}
Message: {user_prompt}

{sessions_text}

IMPORTANT: If the user refers to a previous task (e.g. "刚才那个", "continue", "接着上次"), match it to an existing session and use action "resume" with that session_id. Only use "new" if the user is clearly requesting something unrelated to any existing session."""


@dataclass(frozen=True)
class DispatchResult:
    action: str
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    cwd: Optional[str] = None
    reply_text: Optional[str] = None
    model: Optional[str] = None

    @classmethod
    def from_claude_output(cls, raw_output: str) -> "DispatchResult":
        """Parse the JSON output from claude -p --output-format json."""
        outer = json.loads(raw_output)

        # With --json-schema, Claude puts structured output in "structured_output"
        # Without it, the result is in "result" as a JSON string
        inner = outer.get("structured_output")
        if inner is None:
            result_str = outer.get("result", "{}")
            inner = json.loads(result_str) if isinstance(result_str, str) and result_str else {}

        return cls(
            action=inner.get("action", "reply"),
            session_id=inner.get("session_id"),
            prompt=inner.get("prompt"),
            cwd=inner.get("cwd"),
            reply_text=inner.get("reply_text"),
            model=inner.get("model"),
        )


class Dispatcher:
    def __init__(self, config: ClaudeConfig) -> None:
        self._config = config

    async def dispatch(
        self,
        *,
        user_prompt: str,
        user_email: str,
        sessions: list[dict[str, Any]],
    ) -> DispatchResult:
        """Call Claude Code dispatcher and return structured routing decision."""
        prompt = build_dispatcher_prompt(
            user_prompt=user_prompt,
            user_email=user_email,
            sessions=sessions,
        )

        proc = await asyncio.create_subprocess_exec(
            "claude", "-p", prompt,
            "--model", self._config.dispatcher_model,
            "--json-schema", _DISPATCH_SCHEMA,
            "--system-prompt", _DISPATCHER_SYSTEM_PROMPT,
            "--no-session-persistence",
            "--output-format", "json",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()

        if proc.returncode != 0:
            raise RuntimeError(
                f"Dispatcher claude call failed (exit {proc.returncode}): "
                f"{stderr.decode().strip()}"
            )

        return DispatchResult.from_claude_output(stdout.decode())
