# src/niuma/dispatcher.py
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from dataclasses import dataclass
from typing import Any, Optional

from niuma.config import ClaudeConfig

logger = logging.getLogger(__name__)

_DISPATCHER_SYSTEM_PROMPT = """\
You are the niuma-bot dispatcher. Your ONLY job is to route user messages to one of THREE actions.

You receive:
- The user's message
- The user's email
- Context: known sessions and Claude history

THREE possible actions:

1. "reply" — The user asks a simple factual question you can answer directly WITHOUT running any code, tools, or commands. Examples: "what is 2+2", "what time is it", "explain what niuma-bot is".

2. "new" — The user wants ANYTHING that requires execution: running code, analyzing files, creating things, managing sessions, listing sessions, importing sessions, stopping sessions, scanning history, etc. Basically ANYTHING that is not a trivial factual question. The prompt should faithfully pass through the user's full request. Include cwd if the user mentions a path.

3. "resume" — The user clearly refers to a SPECIFIC existing session (by ID, by "刚才那个", by describing a previous task). Return the session_id and the follow-up prompt.

IMPORTANT: When in doubt between "reply" and "new", choose "new". The worker session has full Claude Code capabilities (file access, shell, tools). You do NOT. Only use "reply" for truly trivial questions.

For "new" actions, set "dedicated_chat" to true ONLY for complex, long-running tasks that will produce lots of output (code review, deep analysis, multi-step projects). Simple commands (ls, echo, quick checks) should set it to false — their results go directly to the main chat.

Return ONLY valid JSON matching the required schema. No other text.\
"""

_DISPATCH_SCHEMA = json.dumps({
    "type": "object",
    "properties": {
        "action": {"enum": ["new", "resume", "reply"]},
        "session_id": {"type": "string"},
        "prompt": {"type": "string"},
        "cwd": {"type": "string"},
        "reply_text": {"type": "string"},
        "model": {"type": "string"},
        "dedicated_chat": {"type": "boolean"},
    },
    "required": ["action"],
})


def build_dispatcher_prompt(
    *,
    user_prompt: str,
    user_email: str,
    sessions: list[dict[str, Any]],
    reply_only: bool = False,
) -> str:
    """Build the prompt sent to the dispatcher Claude session."""
    if sessions:
        session_lines = []
        for s in sessions:
            last_output_preview = ""
            if s.get("last_output"):
                last_output_preview = f' last_result="{s["last_output"][:120]}..."'
            resumable = "YES" if s.get("claude_session") else "NO"
            session_lines.append(
                f"  - [{s['id']}] status={s['status']} resumable={resumable} by={s['created_by']} "
                f"cwd={s.get('cwd', 'N/A')} prompt=\"{s['prompt'][:120]}\""
                f"{last_output_preview}"
            )
        sessions_text = "Known sessions:\n" + "\n".join(session_lines)
    else:
        sessions_text = "No known sessions."

    reply_only_hint = (
        "\n- REPLY-ONLY MODE: This chat is in reply-only mode. You MUST use action \"reply\" and answer directly. "
        "Do NOT use \"new\" or \"resume\"."
        if reply_only else ""
    )

    return f"""\
User: {user_email}
Message: {user_prompt}

{sessions_text}

ROUTING RULES:
- If the user refers to a previous task (e.g. "刚才那个", "continue", "接着上次"), use "resume" with the matching session_id.
- Only resume sessions marked resumable=YES. If resumable=NO, use "new" instead (the worker will handle it).
- For ANYTHING requiring execution (list sessions, scan history, create things, stop sessions, analyze code, etc.) → use "new".
- Only use "reply" for trivial factual questions that need zero tools.
{reply_only_hint}"""


@dataclass(frozen=True)
class DispatchResult:
    action: str
    session_id: Optional[str] = None
    prompt: Optional[str] = None
    cwd: Optional[str] = None
    reply_text: Optional[str] = None
    model: Optional[str] = None
    dedicated_chat: bool = False

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
            dedicated_chat=inner.get("dedicated_chat", False),
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
        reply_only: bool = False,
    ) -> DispatchResult:
        """Call Claude Code dispatcher and return structured routing decision."""
        prompt = build_dispatcher_prompt(
            user_prompt=user_prompt,
            user_email=user_email,
            sessions=sessions,
            reply_only=reply_only,
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
