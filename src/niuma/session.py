# src/niuma/session.py
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from typing import Any, Optional

from niuma.config import ClaudeConfig
from niuma.db import Database

logger = logging.getLogger(__name__)


def _claude_command() -> list[str]:
    """Return the command to invoke Claude Code.

    Uses 'claude' directly. If clp (claude-proxy) has been used before,
    the binary is already patched to support bypassPermissions.
    """
    return ["claude"]

_WORKER_SAFETY_PROMPT = (
    "You are a Claude Code worker session managed by niuma-bot. "
    "Execute the user's request thoroughly. "
    "SAFETY: Do NOT execute destructive commands (rm -rf, git push --force, "
    "DROP TABLE, etc.) unless the user explicitly requests it. "
    "Always prefer safe, reversible operations."
)


class SessionManager:
    def __init__(self, config: ClaudeConfig, db: Database) -> None:
        self._config = config
        self._db = db
        self._active: dict[str, asyncio.subprocess.Process] = {}
        self._tasks: dict[str, asyncio.Task] = {}

    @property
    def active_count(self) -> int:
        return len(self._active)

    async def start_session(
        self,
        *,
        chat_id: str,
        created_by: str,
        prompt: str,
        cwd: Optional[str] = None,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start a new Claude Code worker session."""
        if self.active_count >= self._config.max_concurrent:
            raise RuntimeError(
                f"Max concurrent sessions ({self._config.max_concurrent}) reached"
            )

        work_dir = cwd or self._config.default_cwd
        work_model = model or self._config.worker_model

        session = await self._db.create_session(
            chat_id=chat_id,
            created_by=created_by,
            prompt=prompt,
            cwd=work_dir,
            model=work_model,
        )
        sid = session["id"]

        await self._db.add_message(sid, "user", prompt)
        await self._db.update_session(sid, status="running")

        claude_cmd = _claude_command()
        proc = await asyncio.create_subprocess_exec(
            *claude_cmd, "-p", prompt,
            "--output-format", "json",
            "--name", f"niuma-{created_by.split('@')[0]}-{sid}",
            "--permission-mode", self._config.permission_mode,
            "--model", work_model,
            "--add-dir", work_dir,
            "--append-system-prompt", _WORKER_SAFETY_PROMPT,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=work_dir,
        )

        self._active[sid] = proc
        self._tasks[sid] = asyncio.create_task(
            self._wait_for_completion(sid, proc)
        )

        return session

    async def resume_session(
        self,
        *,
        session_id: str,
        prompt: str,
    ) -> dict[str, Any]:
        """Resume an existing Claude Code session with a follow-up prompt."""
        session = await self._db.get_session(session_id)
        if not session:
            raise ValueError(f"Session {session_id} not found")

        if session_id in self._active:
            raise RuntimeError(f"Session {session_id} is already running")

        if self.active_count >= self._config.max_concurrent:
            raise RuntimeError(
                f"Max concurrent sessions ({self._config.max_concurrent}) reached"
            )

        claude_session_id = session.get("claude_session")
        if not claude_session_id:
            raise ValueError(
                f"Session {session_id} was killed before completing — "
                f"it has no claude session to resume. Please start a new task instead."
            )

        await self._db.add_message(session_id, "user", prompt)
        await self._db.update_session(session_id, status="running")

        claude_cmd = _claude_command()
        resume_cwd = session.get("cwd") or self._config.default_cwd
        proc = await asyncio.create_subprocess_exec(
            *claude_cmd, "-p", prompt,
            "--resume", claude_session_id,
            "--output-format", "json",
            "--permission-mode", self._config.permission_mode,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=resume_cwd,
        )

        self._active[session_id] = proc
        self._tasks[session_id] = asyncio.create_task(
            self._wait_for_completion(session_id, proc)
        )

        return await self._db.get_session(session_id)

    async def stop_session(self, session_id: str) -> None:
        """Kill a running worker session."""
        proc = self._active.get(session_id)
        if proc:
            proc.kill()
            self._cleanup(session_id)
            await self._db.update_session(session_id, status="failed")
            logger.info("Killed session %s", session_id)

    async def get_result(self, session_id: str) -> Optional[dict[str, Any]]:
        """Get session with its current state."""
        return await self._db.get_session(session_id)

    async def list_active(self) -> list[dict[str, Any]]:
        """List all non-terminal sessions."""
        return await self._db.list_sessions()

    async def _wait_for_completion(
        self, session_id: str, proc: asyncio.subprocess.Process
    ) -> None:
        """Wait for a worker subprocess to complete, then update DB."""
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=self._config.session_timeout,
            )

            if proc.returncode == 0:
                output = json.loads(stdout.decode())
                result_text = output.get("result", "")
                claude_sid = output.get("session_id", "")
                cost = output.get("total_cost_usd", 0)

                await self._db.update_session(
                    session_id,
                    status="completed",
                    claude_session=claude_sid,
                    last_output=result_text[:10000],
                    cost_usd=cost,
                )
                await self._db.add_message(session_id, "assistant", result_text)
                logger.info("Session %s completed", session_id)
            else:
                error = stderr.decode().strip()
                await self._db.update_session(
                    session_id, status="failed", last_output=error[:5000]
                )
                logger.error("Session %s failed: %s", session_id, error[:200])

        except asyncio.TimeoutError:
            proc.kill()
            await self._db.update_session(session_id, status="timeout")
            logger.warning("Session %s timed out", session_id)

        except asyncio.CancelledError:
            logger.info("Session %s task cancelled", session_id)

        except Exception as exc:
            await self._db.update_session(
                session_id, status="failed", last_output=str(exc)[:5000]
            )
            logger.exception("Session %s unexpected error", session_id)

        finally:
            self._cleanup(session_id)

    def _cleanup(self, session_id: str) -> None:
        self._active.pop(session_id, None)
        self._tasks.pop(session_id, None)
