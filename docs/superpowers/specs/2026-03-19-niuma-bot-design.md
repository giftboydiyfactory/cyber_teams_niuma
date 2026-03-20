# niuma-bot Design Spec

**Date**: 2026-03-19
**Status**: Draft
**Author**: jackeyw

## Overview

niuma-bot is a self-hosted Teams chat bot that monitors group chats for messages prefixed with `@niuma`, routes them through a Claude Code dispatcher, and manages multiple concurrent Claude Code worker sessions. Each user runs their own instance on their own machine.

## Goals

- Monitor Teams group chats via `teams-cli` polling
- Detect `@niuma` trigger keyword and extract user prompts
- Use a Claude Code "dispatcher session" to interpret user intent and route to appropriate action
- Manage multiple concurrent Claude Code "worker sessions" for task execution
- Report status and results back to Teams chat
- Support session continuity (resume previous sessions with follow-up prompts)
- Distributable: `git clone` + `pip install` + config file

## Non-Goals

- Real-time push notifications (polling-based, not webhook)
- Channel messages (chat only — `teams-cli` cannot write to channels)
- Web UI or dashboard
- Multi-tenant server deployment

## Architecture

### Two-Layer Claude Code Architecture

```
Teams Chat
    │
    ▼
┌─ Poller ─────────────────────────────┐
│  teams-cli chat read (every N sec)   │
│  Filter: new + starts with @niuma    │
│  Dedup: track last processed msg id  │
└──────────┬───────────────────────────┘
           │
           ▼
┌─ Dispatcher ─────────────────────────┐
│  Lightweight Claude Code session     │
│  (sonnet, stateless, json-schema)    │
│                                      │
│  Input: user prompt + session list   │
│  Output: structured routing decision │
│    action: new|resume|reply|         │
│            status|stop|list          │
└──────────┬───────────────────────────┘
           │
           ▼
┌─ SessionManager ─────────────────────┐
│  asyncio subprocess management       │
│  SQLite state persistence            │
│  Concurrent worker limit             │
└──────────┬───────────────────────────┘
           │
           ▼
┌─ Responder ──────────────────────────┐
│  Format output as HTML               │
│  teams-cli chat send --html          │
│  Truncate long output                │
│  AI attribution signature            │
└──────────────────────────────────────┘
```

### Components

#### 1. Poller

- Calls `teams-cli chat read <chat-id> --limit <N> --json` on a configurable interval (default: 60s)
- Monitors one or more chat IDs
- Filters messages starting with the configured trigger prefix (`@niuma`)
- Deduplicates by tracking the last processed `message_id` per chat (persisted in SQLite)
- Extracts sender identity from message metadata

#### 2. Dispatcher

A stateless Claude Code invocation that interprets user intent:

```bash
claude -p "<constructed prompt>" \
  --model sonnet \
  --json-schema '<schema>' \
  --system-prompt "<dispatcher prompt>" \
  --no-session-persistence \
  --output-format json
```

**Dispatcher system prompt** instructs Claude to:
- Receive: user prompt, user identity, list of active sessions (id, status, prompt summary, cwd)
- Decide: is user referring to an existing session, requesting a new task, asking a simple question, or managing sessions
- Return: structured JSON action

**Action semantics**:

| Action | When | Result |
|--------|------|--------|
| `new` | User requests a new task | Start a new worker session |
| `resume` | User refers to an existing session | Send follow-up prompt to that session |
| `reply` | Simple question the dispatcher can answer directly (no worker needed) | Return `reply_text` directly |
| `status` | User asks about a specific session's progress | Bot reports session status |
| `stop` | User wants to terminate a session | Bot kills the worker subprocess |
| `list` | User asks what's running | Bot lists all sessions |

**JSON Schema**:

```json
{
  "type": "object",
  "properties": {
    "action": {
      "enum": ["new", "resume", "reply", "status", "stop", "list"]
    },
    "session_id": {
      "type": "string"
    },
    "prompt": {
      "type": "string"
    },
    "cwd": {
      "type": "string"
    },
    "reply_text": {
      "type": "string"
    },
    "model": {
      "type": "string"
    }
  },
  "required": ["action"]
}
```

#### 3. SessionManager

Manages Claude Code worker subprocess lifecycle:

**Start new session**:
```bash
claude -p "<prompt>" \
  --output-format json \
  --name "niuma-<user>-<short-id>" \
  --permission-mode auto \
  --model <model> \
  --add-dir <cwd>
```

**Resume session**:
```bash
claude -p "<prompt>" \
  --resume <claude_session_id> \
  --output-format json
```

**Concurrency**: `asyncio.create_subprocess_exec` with configurable max concurrent workers (default: 5). Excess requests queued.

**Timeout**: Configurable, default 24 hours. Subprocess killed and marked `timeout` on expiry.

#### 4. Responder

- Formats Claude output as HTML for Teams readability
- Uses `READ_WRITE_MODE=1 teams-cli chat send <chat-id> --html --body "<content>"`
- Truncation: output < 2000 chars sent directly; longer output sends summary + saves full output to `~/.niuma/outputs/<session-id>.md`
- Always appends AI attribution: `Sent via Claude Code (ai-pim-utils)`
- Includes session ID in responses for user reference

### Progress Flow

```
User sends @niuma prompt
    │
    ▼
Bot immediately replies: "⏳ session [<id>] processing..."
    │
    ▼
asyncio awaits claude subprocess in background
    │
    ├─ success → "✅ session [<id>] complete\n<result>"
    ├─ failure → "❌ session [<id>] failed\n<error>"
    └─ timeout → "⏰ session [<id>] timed out after 24h"
```

## Data Model

### SQLite Schema

```sql
CREATE TABLE sessions (
    id              TEXT PRIMARY KEY,    -- short ID (e.g. "a3f7")
    claude_session  TEXT,                -- claude CLI session_id (UUID)
    chat_id         TEXT NOT NULL,       -- source Teams chat ID
    created_by      TEXT NOT NULL,       -- sender email
    status          TEXT NOT NULL,       -- pending/running/completed/failed/timeout
    cwd             TEXT,                -- working directory
    model           TEXT,                -- model used
    prompt          TEXT,                -- original prompt
    last_output     TEXT,                -- most recent output (truncated)
    cost_usd        REAL DEFAULT 0,      -- cumulative cost
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT NOT NULL REFERENCES sessions(id),
    role            TEXT NOT NULL,       -- user / assistant
    content         TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE poll_state (
    chat_id         TEXT PRIMARY KEY,
    last_message_id TEXT,
    updated_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### Session State Machine

```
pending → running → completed
                  → failed
                  → timeout
         running ← (resumed from completed/failed)
```

## Configuration

```yaml
# ~/.niuma/config.yaml

teams:
  chat_ids:
    - "19:xxx@thread.v2"
  trigger: "@niuma"
  poll_interval: 60          # seconds

claude:
  dispatcher_model: "sonnet"
  worker_model: "sonnet"
  max_concurrent: 5
  session_timeout: 86400     # 24 hours
  permission_mode: "auto"
  default_cwd: "~"

security:
  allowed_users:
    - "jackeyw@nvidia.com"
  admin_users:
    - "jackeyw@nvidia.com"

storage:
  db_path: "~/.niuma/niuma.db"

logging:
  level: "INFO"
  file: "~/.niuma/niuma.log"
```

## Security

### Access Control

| Operation | Requirement |
|-----------|-------------|
| Start new session | `allowed_users` |
| View own sessions | `allowed_users` |
| Resume own session | `allowed_users` |
| Stop own session | `allowed_users` |
| View all sessions | `admin_users` |
| Stop others' sessions | `admin_users` |
| Unauthorized user | Message ignored silently |

### Claude Code Worker Safety

- `--permission-mode auto` for unattended execution
- `--add-dir <cwd>` limits filesystem access scope
- Worker system prompt includes safety constraints (no destructive commands unless explicitly requested)
- `--disallowed-tools` can restrict specific tools per config

### Error Handling

| Failure | Behavior |
|---------|----------|
| `teams-cli` auth expired (exit 2) | Log error, skip poll cycle, notify admin on next successful cycle |
| `teams-cli` rate limited (exit 5) | Backoff: wait `retry_after_seconds`, then resume polling |
| `teams-cli` network error (exit 7) | Exponential backoff (1s, 2s, 4s, ..., max 5min), resume on success |
| `claude` CLI not found | Fatal: exit with clear error message at startup |
| Dispatcher call fails | Log error, skip this message, retry on next poll cycle |

### Sensitive Data

- No secrets in config.yaml
- Teams auth managed by `teams-cli` (Entra device code flow)
- Claude auth managed by `claude` CLI
- Logs exclude message body content; only session_id and action logged

## Project Structure

```
niuma-bot/
├── pyproject.toml
├── config.yaml.example
├── src/
│   └── niuma/
│       ├── __init__.py
│       ├── main.py         # entry point, asyncio main loop
│       ├── poller.py       # Teams message polling
│       ├── dispatcher.py   # Dispatcher Claude session
│       ├── session.py      # Worker session management
│       ├── responder.py    # Teams reply formatting + sending
│       ├── db.py           # SQLite operations
│       └── config.py       # Configuration loading
├── tests/
│   ├── test_poller.py
│   ├── test_dispatcher.py
│   ├── test_session.py
│   ├── test_responder.py
│   └── test_db.py
├── docs/
│   └── superpowers/
│       └── specs/
│           └── 2026-03-19-niuma-bot-design.md
└── README.md
```

## Dependencies

- **Python 3.9+**
- **pyyaml** — config parsing
- **aiosqlite** — async SQLite
- **External**: `teams-cli` (installed), `claude` CLI (installed)

## Installation & Usage

```bash
git clone <repo>
cd niuma-bot
pip install -e .
cp config.yaml.example ~/.niuma/config.yaml
# Edit config: add chat_ids, allowed_users

niuma              # foreground
niuma --daemon     # background
```

## Interaction Example

```
Jack:  @niuma 帮我看看 /home/scratch/repo-x 最近的 commit 有没有性能问题
Bot:   ⏳ 已收到，session [a3f7] 处理中...
Bot:   ✅ session [a3f7] 完成
       发现 2 个潜在性能问题：
       1. src/cache.py:42 — 缓存未设过期时间
       2. src/query.py:118 — N+1 查询问题

Jack:  @niuma 帮我修一下刚才说的那两个问题
       → Dispatcher routes to resume session [a3f7]
Bot:   ⏳ session [a3f7] 继续处理中...
Bot:   ✅ session [a3f7] 完成
       已修复 src/cache.py 和 src/query.py

Alice: @niuma 帮我在 /home/scratch/repo-y 写个 unit test for utils.py
Bot:   ⏳ 已收到，session [b2e1] 处理中...

Jack:  @niuma 现在有几个任务在跑
       → Dispatcher returns list action
Bot:   📋 当前 sessions:
       [a3f7] ✅ completed — Jack — 性能分析 repo-x
       [b2e1] 🔄 running  — Alice — unit test repo-y
```
