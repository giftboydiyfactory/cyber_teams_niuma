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
from niuma.teams_api import create_session_chat
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
        """Single poll cycle across all configured chats + session chats."""
        for chat_id in self._config.teams.chat_ids:
            await self._poll_chat(chat_id)

        # Also poll session-dedicated chats
        if self._config.teams.auto_session_chats:
            session_chat_ids = await self._db.list_session_chat_ids()
            for chat_id in session_chat_ids:
                await self._poll_session_chat(chat_id)

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

            await self._handle_message(chat_id, msg.sender_email, prompt, msg.id)

        await self._db.set_poll_state(chat_id, newest_id)

    async def _poll_session_chat(self, chat_id: str) -> None:
        """Poll a session-dedicated chat. All messages auto-route to the bound session."""
        from niuma.poller import TeamsCliError

        try:
            raw = await self._poller.poll_chat(chat_id)
        except (TeamsCliError, RuntimeError):
            return

        messages = self._poller.parse_messages(raw)
        if not messages:
            return

        last_seen = await self._db.get_poll_state(chat_id)

        try:
            newest_id = max(messages, key=lambda m: int(m.id)).id
        except ValueError:
            newest_id = messages[0].id

        # Filter new messages (no trigger needed in session chats)
        new_messages = self._poller.filter_new(messages, last_seen)
        if not new_messages:
            await self._db.set_poll_state(chat_id, newest_id)
            return

        # Find the bound session
        session = await self._db.get_session_by_chat_id(chat_id)
        if not session:
            await self._db.set_poll_state(chat_id, newest_id)
            return

        for msg in new_messages:
            if not self._is_allowed(msg.sender_email):
                continue

            # Skip bot's own messages (contain the signature)
            if "Sent via Claude Code" in msg.body:
                continue

            prompt = msg.body.strip()
            if not prompt:
                continue

            # Auto-resume the bound session
            sid = session["id"]
            logger.info("Session chat %s: routing to session [%s]", chat_id[:20], sid)

            try:
                await self._session_mgr.resume_session(
                    session_id=sid, prompt=prompt,
                )
                await self._responder.send_processing(chat_id, sid)
                asyncio.create_task(self._watch_session(chat_id, sid))
            except (ValueError, RuntimeError) as e:
                await self._responder.send_text(chat_id, str(e))

        await self._db.set_poll_state(chat_id, newest_id)

    def _is_reply_only(self, chat_id: str) -> bool:
        return chat_id in self._config.teams.reply_only_chat_ids

    async def _handle_message(
        self, chat_id: str, user_email: str, prompt: str,
        message_id: str = "",
    ) -> None:
        """Dispatch a single user message through the Claude dispatcher.

        The dispatcher only decides: new, resume, or reply.
        All complex operations (list, scan, import, stop, etc.) are handled
        by worker sessions — not hardcoded in Python.
        """
        reply_only = self._is_reply_only(chat_id)
        rt = message_id

        try:
            sessions = await self._session_mgr.list_active()
            dispatch = await self._dispatcher.dispatch(
                user_prompt=prompt,
                user_email=user_email,
                sessions=sessions,
                reply_only=reply_only,
            )
        except Exception:
            logger.exception("Dispatcher failed for message from %s", user_email)
            return

        logger.info("Dispatch: action=%s for user=%s", dispatch.action, user_email)

        # Enforce reply-only mode
        if reply_only and dispatch.action != "reply":
            await self._responder.send_text(
                chat_id,
                dispatch.reply_text or "This chat is in reply-only mode.",
                reply_to=rt,
            )
            return

        if dispatch.action == "new":
            await self._handle_new(chat_id, user_email, dispatch, rt)
        elif dispatch.action == "resume":
            await self._handle_resume(chat_id, dispatch, rt, user_email=user_email)
        elif dispatch.action == "reply":
            await self._responder.send_text(chat_id, dispatch.reply_text or "", reply_to=rt)

    async def _handle_new(
        self, chat_id: str, user_email: str, dispatch: DispatchResult,
        reply_to: str = "",
    ) -> None:
        try:
            session = await self._session_mgr.start_session(
                chat_id=chat_id,
                created_by=user_email,
                prompt=dispatch.prompt or "",
                cwd=dispatch.cwd,
                model=dispatch.model,
                trigger_message_id=reply_to,
            )
            sid = session["id"]
            session_chat_id = None

            # Only create dedicated chat for complex tasks
            if dispatch.dedicated_chat:
                try:
                    prompt_preview = (dispatch.prompt or "")[:50]
                    chat_info = create_session_chat(
                        session_id=sid,
                        topic=prompt_preview,
                        user_email=user_email,
                    )
                    session_chat_id = chat_info["chat_id"]
                    await self._db.update_session(sid, session_chat_id=session_chat_id)

                    web_url = chat_info["web_url"]
                    await self._responder.send_text(
                        chat_id,
                        f"🚀 session [{sid}] started → [open session chat]({web_url})",
                        reply_to=reply_to,
                    )
                    await self._responder.send_processing(session_chat_id, sid)
                except Exception as e:
                    logger.warning("Failed to create session chat: %s. Using main chat.", e)
                    session_chat_id = None

            if not session_chat_id:
                await self._responder.send_processing(chat_id, sid, reply_to=reply_to)

            # Watch session — send results to session chat if available, else main chat
            output_chat = session_chat_id or chat_id
            asyncio.create_task(
                self._watch_session(output_chat, sid, reply_to="" if session_chat_id else reply_to)
            )
        except RuntimeError as e:
            await self._responder.send_text(chat_id, str(e), reply_to=reply_to)

    async def _handle_resume(
        self, chat_id: str, dispatch: DispatchResult,
        reply_to: str = "", user_email: str = "",
    ) -> None:
        sid = dispatch.session_id
        if not sid:
            await self._responder.send_text(chat_id, "No session ID to resume.", reply_to=reply_to)
            return

        session = await self._db.get_session(sid)
        if not session:
            await self._responder.send_text(
                chat_id, f"Session {sid} not found.",
                reply_to=reply_to,
            )
            return

        actual_sid = session["id"]
        # Route output to session's dedicated chat if it has one
        session_chat_id = session.get("session_chat_id")
        output_chat = session_chat_id or chat_id
        output_reply_to = "" if session_chat_id else reply_to

        try:
            await self._session_mgr.resume_session(
                session_id=actual_sid, prompt=dispatch.prompt or ""
            )
            if session_chat_id:
                await self._responder.send_text(
                    chat_id, f"🔄 session [{actual_sid}] resuming → results in session chat",
                    reply_to=reply_to,
                )
            await self._responder.send_processing(output_chat, actual_sid, reply_to=output_reply_to)
            asyncio.create_task(self._watch_session(output_chat, actual_sid, output_reply_to))
        except (ValueError, RuntimeError) as e:
            await self._responder.send_text(chat_id, str(e), reply_to=reply_to)

    async def _watch_session(
        self, chat_id: str, session_id: str, reply_to: str = "",
    ) -> None:
        """Poll DB until session completes, then send result as thread reply."""
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
                        reply_to=reply_to,
                    )
                elif status == "timeout":
                    await self._responder.send_result(
                        chat_id, session_id,
                        error="Session timed out (24h)",
                        reply_to=reply_to,
                    )
                else:
                    await self._responder.send_result(
                        chat_id, session_id,
                        error=session.get("last_output", "Unknown error"),
                        reply_to=reply_to,
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
