# src/niuma/scanner.py
"""Scan ~/.claude/projects/ to discover all Claude Code sessions across directories."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _decode_project_path(encoded: str) -> str:
    """Convert encoded project dir name back to filesystem path.

    Claude encodes paths by replacing / with -.
    E.g. /home/jackeyw -> -home-jackeyw
    Ambiguity: directory names with dashes are indistinguishable from separators.
    Strategy: greedily try longest matching path segments from left to right.
    """
    parts = encoded.lstrip("-").split("-")
    path = "/"
    i = 0
    while i < len(parts):
        # Try longest match first: join remaining parts progressively
        found = False
        for j in range(len(parts), i, -1):
            candidate = path + "-".join(parts[i:j])
            if Path(candidate).exists():
                path = candidate + "/"
                i = j
                found = True
                break
        if not found:
            # Fallback: treat each part as a directory
            path += parts[i] + "/"
            i += 1
    return path.rstrip("/") or "/"


def _read_session_meta(jsonl_path: Path) -> Optional[dict[str, Any]]:
    """Read minimal metadata from a session .jsonl file."""
    try:
        session_id = jsonl_path.stem
        stat = jsonl_path.stat()

        title = ""
        first_user_msg = ""
        last_user_msg = ""
        num_user_turns = 0
        first_ts = None
        last_ts = stat.st_mtime
        session_cwd = None

        with open(jsonl_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = entry.get("timestamp")
                if ts and first_ts is None:
                    first_ts = ts

                entry_type = entry.get("type", "")

                # Extract title
                if entry_type == "custom-title":
                    title = entry.get("customTitle", "") or title

                # Extract cwd from any entry that has it
                if entry.get("cwd") and not session_cwd:
                    session_cwd = entry["cwd"]

                # Extract user messages
                if entry_type == "user":
                    msg = entry.get("message", {})
                    content = msg.get("content", "")
                    if isinstance(content, list):
                        texts = [
                            b.get("text", "")
                            for b in content
                            if isinstance(b, dict) and b.get("type") == "text"
                        ]
                        content = " ".join(texts)
                    if content:
                        content = str(content)[:200]
                        if not first_user_msg:
                            first_user_msg = content
                        last_user_msg = content
                        num_user_turns += 1

        if num_user_turns == 0:
            return None

        return {
            "claude_session": session_id,
            "name": title,
            "first_user_msg": first_user_msg,
            "last_user_msg": last_user_msg,
            "num_turns": num_user_turns,
            "created_at": first_ts or stat.st_ctime,
            "updated_at": last_ts,
            "session_cwd": session_cwd,
        }
    except Exception as e:
        logger.debug("Failed to read session %s: %s", jsonl_path.name, e)
        return None


def scan_all_sessions() -> list[dict[str, Any]]:
    """Scan all Claude Code sessions across all project directories."""
    if not _CLAUDE_PROJECTS_DIR.exists():
        return []

    results = []
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue

        cwd = _decode_project_path(project_dir.name)

        for jsonl_file in project_dir.glob("*.jsonl"):
            meta = _read_session_meta(jsonl_file)
            if meta:
                # Prefer cwd from session data, fallback to decoded path
                meta["cwd"] = meta.pop("session_cwd", None) or cwd
                results.append(meta)

    results.sort(key=lambda s: s["updated_at"], reverse=True)
    return results


def scan_sessions_summary(limit: int = 20) -> str:
    """Return a formatted summary of all discovered Claude sessions."""
    sessions = scan_all_sessions()
    if not sessions:
        return "No Claude Code sessions found."

    lines = [f"Found {len(sessions)} Claude Code sessions:\n"]
    for s in sessions[:limit]:
        age_hours = (time.time() - s["updated_at"]) / 3600
        if age_hours < 1:
            age_str = f"{int(age_hours * 60)}m ago"
        elif age_hours < 24:
            age_str = f"{int(age_hours)}h ago"
        else:
            age_str = f"{int(age_hours / 24)}d ago"

        name = s["name"] or s["first_user_msg"][:40] or "unnamed"
        sid_short = s["claude_session"][:12]

        lines.append(
            f"[{sid_short}] {name} ({s['num_turns']} turns, {age_str})\n"
            f"  dir: {s['cwd']}"
        )

    if len(sessions) > limit:
        lines.append(f"\n... and {len(sessions) - limit} more")

    return "\n".join(lines)
