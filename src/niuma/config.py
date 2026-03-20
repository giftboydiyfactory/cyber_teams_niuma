from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


class ConfigError(Exception):
    """Configuration loading or validation error."""


@dataclass(frozen=True)
class TeamsConfig:
    chat_ids: list[str]
    trigger: str
    poll_interval: int
    reply_only_chat_ids: list[str]
    auto_session_chats: bool


@dataclass(frozen=True)
class ClaudeConfig:
    dispatcher_model: str
    worker_model: str
    max_concurrent: int
    session_timeout: int
    permission_mode: str
    default_cwd: str


@dataclass(frozen=True)
class SecurityConfig:
    allowed_users: list[str]
    admin_users: list[str]


@dataclass(frozen=True)
class StorageConfig:
    db_path: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str
    file: str


@dataclass(frozen=True)
class NiumaConfig:
    teams: TeamsConfig
    claude: ClaudeConfig
    security: SecurityConfig
    storage: StorageConfig
    logging: LoggingConfig


def _expand_path(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def _require(data: dict[str, Any], key: str, context: str) -> Any:
    if key not in data:
        raise ConfigError(f"Missing required field '{key}' in {context}")
    return data[key]


def load_config(path: Path) -> NiumaConfig:
    """Load and validate configuration from a YAML file."""
    if not path.exists():
        raise ConfigError(f"Config file not found: {path}")

    with open(path) as f:
        raw = yaml.safe_load(f)

    if not isinstance(raw, dict):
        raise ConfigError("Config file must be a YAML mapping")

    teams_raw = _require(raw, "teams", "root")
    claude_raw = _require(raw, "claude", "root")
    security_raw = _require(raw, "security", "root")
    storage_raw = _require(raw, "storage", "root")
    logging_raw = _require(raw, "logging", "root")

    poll_interval = teams_raw.get("poll_interval", 60)
    if poll_interval < 5:
        raise ConfigError("teams.poll_interval must be >= 5 seconds")

    max_concurrent = claude_raw.get("max_concurrent", 5)
    if max_concurrent < 1:
        raise ConfigError("claude.max_concurrent must be >= 1")

    session_timeout = claude_raw.get("session_timeout", 86400)
    if session_timeout < 60:
        raise ConfigError("claude.session_timeout must be >= 60 seconds")

    return NiumaConfig(
        teams=TeamsConfig(
            chat_ids=_require(teams_raw, "chat_ids", "teams"),
            trigger=teams_raw.get("trigger", "@niuma"),
            poll_interval=poll_interval,
            reply_only_chat_ids=teams_raw.get("reply_only_chat_ids", []),
            auto_session_chats=teams_raw.get("auto_session_chats", True),
        ),
        claude=ClaudeConfig(
            dispatcher_model=claude_raw.get("dispatcher_model", "sonnet"),
            worker_model=claude_raw.get("worker_model", "sonnet"),
            max_concurrent=max_concurrent,
            session_timeout=session_timeout,
            permission_mode=claude_raw.get("permission_mode", "auto"),
            default_cwd=_expand_path(claude_raw.get("default_cwd", "~")),
        ),
        security=SecurityConfig(
            allowed_users=_require(security_raw, "allowed_users", "security"),
            admin_users=security_raw.get("admin_users", []),
        ),
        storage=StorageConfig(
            db_path=_expand_path(_require(storage_raw, "db_path", "storage")),
        ),
        logging=LoggingConfig(
            level=logging_raw.get("level", "INFO"),
            file=_expand_path(logging_raw.get("file", "~/.niuma/niuma.log")),
        ),
    )
