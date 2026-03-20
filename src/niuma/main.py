from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

from niuma.config import NiumaConfig, load_config, ConfigError
from niuma.db import Database
from niuma.dispatcher import Dispatcher, DispatchResult
from niuma.poller import Poller
from niuma.scanner import scan_all_sessions, scan_sessions_summary
from niuma.responder import Responder
from niuma.session import SessionManager

logger = logging.getLogger("niuma")

_DEFAULT_CONFIG = Path.home() / ".niuma" / "config.yaml"


class NiumaBot:
    def __init__(self, config: NiumaConfig) -> None:
        self._config = config
        self._db: Optional[Database] = None
        self._poller: Optional[Poller] = None
        self._dispatcher: Optional[Dispatcher] = None
        self._session_mgr: Optional[SessionManager] = None
        self._responder: Optional[Responder] = None
        self._running = False
        self._backoff_seconds = 0

    async def init(self) -> None:
        self._db = Database(self._config.storage.db_path)
        await self._db.init()
        self._poller = Poller(self._config.teams)
        self._dispatcher = Dispatcher(self._config.claude)
        self._session_mgr = SessionManager(self._config.claude, self._db)
        self._responder = Responder()

    async def shutdown(self) -> None:
        self._running = False
        if self._db:
            await self._db.close()

    async def run(self) -> None:
        """Main polling loop."""
        self._running = True
        logger.info(
            "niuma-bot started, polling every %ds",
            self._config.teams.poll_interval,
        )

        while self._running:
            try:
                await self.poll_once()
            except Exception:
                logger.exception("Error in poll cycle")
            await asyncio.sleep(self._config.teams.poll_interval)

    async def poll_once(self) -> None:
        """Single poll cycle across all configured chats."""
        for chat_id in self._config.teams.chat_ids:
            await self._poll_chat(chat_id)

    async def _poll_chat(self, chat_id: str) -> None:
        from niuma.poller import TeamsCliError

        try:
            raw = await self._poller.poll_chat(chat_id)
            self._backoff_seconds = 0  # reset on success
        except TeamsCliError as e:
            if e.exit_code == 5:  # rate limited
                logger.warning("Rate limited on %s, backing off", chat_id)
                await asyncio.sleep(min(self._backoff_seconds or 30, 300))
                return
            elif e.exit_code == 7:  # network error
                self._backoff_seconds = min((self._backoff_seconds or 1) * 2, 300)
                logger.warning("Network error on %s, backoff %ds", chat_id, self._backoff_seconds)
                await asyncio.sleep(self._backoff_seconds)
                return
            elif e.exit_code == 2:  # auth expired
                logger.error("Auth expired for teams-cli, skipping cycle")
                return
            else:
                logger.error("Poll failed for %s: %s", chat_id, e)
                return
        except RuntimeError as e:
            logger.error("Poll failed for %s: %s", chat_id, e)
            return

        messages = self._poller.parse_messages(raw)
        if not messages:
            return

        last_seen = await self._db.get_poll_state(chat_id)
        triggered = self._poller.filter_triggered(messages)
        new_messages = self._poller.filter_new(triggered, last_seen)

        # Find the newest message ID (messages may be newest-first from API)
        try:
            newest_id = max(messages, key=lambda m: int(m.id)).id
        except ValueError:
            newest_id = messages[0].id

        if not new_messages:
            await self._db.set_poll_state(chat_id, newest_id)
            return

        for msg in new_messages:
            if not self._is_allowed(msg.sender_email):
                logger.info(
                    "Ignoring message from unauthorized user: %s",
                    msg.sender_email,
                )
                continue

            prompt = self._poller.extract_prompt(msg)
            if not prompt:
                continue

            await self._handle_message(chat_id, msg.sender_email, prompt)

        await self._db.set_poll_state(chat_id, newest_id)

    async def _handle_message(
        self, chat_id: str, user_email: str, prompt: str
    ) -> None:
        """Dispatch a single user message through the Claude dispatcher."""
        try:
            sessions = await self._session_mgr.list_active()
            dispatch = await self._dispatcher.dispatch(
                user_prompt=prompt,
                user_email=user_email,
                sessions=sessions,
            )
        except Exception:
            logger.exception("Dispatcher failed for message from %s", user_email)
            return

        logger.info("Dispatch: action=%s for user=%s", dispatch.action, user_email)

        if dispatch.action == "new":
            await self._handle_new(chat_id, user_email, dispatch)
        elif dispatch.action == "resume":
            await self._handle_resume(chat_id, dispatch)
        elif dispatch.action == "reply":
            await self._responder.send_text(chat_id, dispatch.reply_text or "")
        elif dispatch.action == "list":
            await self._handle_list(chat_id, user_email)
        elif dispatch.action == "status":
            await self._handle_status(chat_id, user_email, dispatch)
        elif dispatch.action == "stop":
            await self._handle_stop(chat_id, user_email, dispatch)
        elif dispatch.action == "scan_all":
            await self._handle_scan_all(chat_id)
        elif dispatch.action == "import":
            await self._handle_import(chat_id, user_email, dispatch)

    async def _handle_scan_all(self, chat_id: str) -> None:
        summary = scan_sessions_summary(limit=15)
        await self._responder.send_text(chat_id, summary)

    async def _handle_import(
        self, chat_id: str, user_email: str, dispatch: DispatchResult
    ) -> None:
        """Import an external Claude session and optionally resume it."""
        claude_sid = dispatch.session_id
        if not claude_sid:
            await self._responder.send_text(chat_id, "No session ID provided to import.")
            return

        # Find the session in filesystem
        all_sessions = scan_all_sessions()
        match = None
        for s in all_sessions:
            if s["claude_session"].startswith(claude_sid):
                match = s
                break

        if not match:
            await self._responder.send_text(
                chat_id, f"Session {claude_sid} not found in Claude history."
            )
            return

        # Import into DB
        imported = await self._db.import_session(
            claude_session=match["claude_session"],
            chat_id=chat_id,
            created_by=user_email,
            prompt=match.get("last_user_msg") or match.get("name") or "imported session",
            cwd=match["cwd"],
        )

        sid = imported["id"]
        prompt = dispatch.prompt

        if prompt:
            # Import and resume in one step
            try:
                await self._session_mgr.resume_session(
                    session_id=sid, prompt=prompt,
                )
                await self._responder.send_processing(chat_id, sid)
                asyncio.create_task(self._watch_session(chat_id, sid))
            except (ValueError, RuntimeError) as e:
                await self._responder.send_text(chat_id, str(e))
        else:
            name = match.get("name") or "unnamed"
            await self._responder.send_text(
                chat_id,
                f"Imported session [{sid}] (was {match['claude_session'][:12]}...)\n"
                f"Name: {name}\n"
                f"Dir: {match['cwd']}\n"
                f"Turns: {match['num_turns']}\n\n"
                f"You can now resume it with: @niuma 继续 session {sid} <your request>"
            )

    async def _handle_new(
        self, chat_id: str, user_email: str, dispatch: DispatchResult
    ) -> None:
        try:
            session = await self._session_mgr.start_session(
                chat_id=chat_id,
                created_by=user_email,
                prompt=dispatch.prompt or "",
                cwd=dispatch.cwd,
                model=dispatch.model,
            )
            await self._responder.send_processing(chat_id, session["id"])
            asyncio.create_task(
                self._watch_session(chat_id, session["id"])
            )
        except RuntimeError as e:
            await self._responder.send_text(chat_id, str(e))

    async def _handle_resume(
        self, chat_id: str, dispatch: DispatchResult
    ) -> None:
        sid = dispatch.session_id
        if not sid:
            await self._responder.send_text(chat_id, "No session ID to resume.")
            return
        try:
            await self._session_mgr.resume_session(
                session_id=sid, prompt=dispatch.prompt or ""
            )
            await self._responder.send_processing(chat_id, sid)
            asyncio.create_task(self._watch_session(chat_id, sid))
        except (ValueError, RuntimeError) as e:
            await self._responder.send_text(chat_id, str(e))

    async def _handle_list(
        self, chat_id: str, user_email: str
    ) -> None:
        is_admin = user_email in self._config.security.admin_users
        if is_admin:
            sessions = await self._session_mgr.list_active()
        else:
            all_sessions = await self._session_mgr.list_active()
            sessions = [s for s in all_sessions if s["created_by"] == user_email]
        await self._responder.send_session_list(chat_id, sessions)

    async def _handle_status(
        self, chat_id: str, user_email: str, dispatch: DispatchResult
    ) -> None:
        sid = dispatch.session_id
        if sid:
            session = await self._session_mgr.get_result(sid)
            if session:
                await self._responder.send_status(chat_id, session)
            else:
                await self._responder.send_text(
                    chat_id, f"Session {sid} not found."
                )
        else:
            await self._handle_list(chat_id, user_email)

    async def _handle_stop(
        self, chat_id: str, user_email: str, dispatch: DispatchResult
    ) -> None:
        sid = dispatch.session_id
        if not sid:
            await self._responder.send_text(chat_id, "No session ID to stop.")
            return

        session = await self._session_mgr.get_result(sid)
        if not session:
            await self._responder.send_text(
                chat_id, f"Session {sid} not found."
            )
            return

        is_owner = session["created_by"] == user_email
        is_admin = user_email in self._config.security.admin_users
        if not (is_owner or is_admin):
            await self._responder.send_text(
                chat_id,
                f"Permission denied: you don't own session {sid}.",
            )
            return

        await self._session_mgr.stop_session(sid)
        await self._responder.send_text(chat_id, f"Session {sid} stopped.")

    async def _watch_session(self, chat_id: str, session_id: str) -> None:
        """Poll DB until session completes, then send result."""
        while True:
            await asyncio.sleep(2)
            session = await self._session_mgr.get_result(session_id)
            if not session:
                return
            status = session["status"]
            if status in ("completed", "failed", "timeout"):
                if status == "completed":
                    await self._responder.send_result(
                        chat_id, session_id,
                        result=session.get("last_output"),
                    )
                elif status == "timeout":
                    await self._responder.send_result(
                        chat_id, session_id,
                        error="Session timed out (24h)",
                    )
                else:
                    await self._responder.send_result(
                        chat_id, session_id,
                        error=session.get("last_output", "Unknown error"),
                    )
                return

    def _is_allowed(self, email: str) -> bool:
        return (
            email in self._config.security.allowed_users
            or email in self._config.security.admin_users
        )


def _setup_logging(config: NiumaConfig) -> None:
    log_path = Path(config.logging.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=getattr(logging, config.logging.level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path)),
            logging.StreamHandler(),
        ],
    )


def cli_entry() -> None:
    parser = argparse.ArgumentParser(
        description="niuma-bot: Teams chat bot powered by Claude Code"
    )
    parser.add_argument(
        "-c", "--config",
        default=str(_DEFAULT_CONFIG),
        help=f"Config file path (default: {_DEFAULT_CONFIG})",
    )
    parser.add_argument(
        "--daemon", action="store_true",
        help="Run in background (nohup-style)",
    )
    args = parser.parse_args()

    try:
        config = load_config(Path(args.config))
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    _setup_logging(config)

    if args.daemon:
        _daemonize()

    bot = NiumaBot(config)

    async def _run() -> None:
        await bot.init()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(
                sig, lambda: asyncio.create_task(bot.shutdown())
            )
        await bot.run()

    asyncio.run(_run())


def _daemonize() -> None:
    """Simple double-fork daemonization."""
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdin = open(os.devnull)
