---
name: ai-pim-utils-guide
description: "This skill should be used when the user asks to 'send an email', 'send a Teams message', 'reply to a chat', 'create a calendar event', 'write to Confluence', or any write/send operation via ai-pim-utils CLIs (outlook-cli, teams-cli, calendar-cli, confluence-cli, etc.). Also use when the user mentions READ_WRITE_MODE, write mode, or asks why a send/write command fails with READ_ONLY_MODE error."
---

# ai-pim-utils Write Mode Guide

The ai-pim-utils CLI suite defaults to **read-only mode** for safety. Write operations (send, create, delete, reply, etc.) are blocked unless explicitly enabled.

## Enabling Write Mode

Set the `READ_WRITE_MODE` environment variable to `1` before running write commands:

```bash
export READ_WRITE_MODE=1
```

Or prefix individual commands:

```bash
READ_WRITE_MODE=1 teams-cli chat send "<chat-id>" --body "Hello"
READ_WRITE_MODE=1 outlook-cli message reply <message-id> --body "Got it, thanks."
READ_WRITE_MODE=1 calendar-cli create --subject "Sync" --start 2026-03-20T10:00:00 --end 2026-03-20T10:30:00
```

Without this variable, write commands return:
```json
{
  "error": {
    "code": "READ_ONLY_MODE",
    "message": "This operation is not available in read-only mode"
  }
}
```

## Write Operations by CLI

### teams-cli

```bash
# Send message to a chat
READ_WRITE_MODE=1 teams-cli chat send "<chat-id>" --body "Message text"

# Reply to a specific message
READ_WRITE_MODE=1 teams-cli chat reply "<chat-id>" --reply-to "<message-id>" --body "Reply text"
```

To find the chat-id, first list chats:
```bash
teams-cli chat list --json
```

Or read a specific chat to get message IDs:
```bash
teams-cli chat read "<chat-id>" --limit 10 --json
```

### outlook-cli

Outlook uses a **draft-based** workflow — compose operations create drafts that must be explicitly sent:

```bash
# Create a reply draft (does NOT send)
READ_WRITE_MODE=1 outlook-cli message reply <message-id> --body "Reply content"

# Forward as draft (does NOT send)
READ_WRITE_MODE=1 outlook-cli message forward <message-id> --to recipient@example.com --body "FYI"

# Create a new draft
READ_WRITE_MODE=1 outlook-cli message draft --to recipient@example.com --subject "Subject" --body "Body"

# Organize operations (also require write mode)
READ_WRITE_MODE=1 outlook-cli message move <message-id> --folder "Archive"
READ_WRITE_MODE=1 outlook-cli message delete <message-id>
READ_WRITE_MODE=1 outlook-cli message flag <message-id>
READ_WRITE_MODE=1 outlook-cli message mark <message-id> --read
```

### calendar-cli

```bash
# Create event
READ_WRITE_MODE=1 calendar-cli create --subject "Meeting" \
  --start 2026-03-20T14:00:00 --end 2026-03-20T15:00:00 \
  --attendees user1@nvidia.com,user2@nvidia.com

# Respond to event
READ_WRITE_MODE=1 calendar-cli respond <event-id> --accept
READ_WRITE_MODE=1 calendar-cli respond <event-id> --decline

# Delete event
READ_WRITE_MODE=1 calendar-cli delete <event-id>
```

### confluence-cli

```bash
# Create page
READ_WRITE_MODE=1 confluence-cli page create --space ENG --title "Page Title" --body "<p>Content</p>"

# Update page
READ_WRITE_MODE=1 confluence-cli page update <page-id> --body "<p>Updated content</p>"

# Add comment
READ_WRITE_MODE=1 confluence-cli comment add <page-id> --body "Comment text"
```

## Message Formatting Rules

### AI Attribution (Mandatory)

Every message sent by AI **must** append a signature line at the end:

```
---
Sent via Claude Code (ai-pim-utils)
```

For Teams messages, append with a blank line before the separator. For emails, append at the bottom of the body.

### Readability (Mandatory)

**Teams messages must use `--html` flag** — plain text in Teams does not render any formatting (no bold, no lists, no line breaks). Always send with `--html` and use HTML tags.

**Be concise** — recipients don't want walls of text. Lead with the conclusion, use bullet points for details, skip unnecessary explanation. If it can be said in 3 bullets, don't use 6 paragraphs.

Format all outgoing messages for human readability:

- Use `<p>` for paragraphs with spacing
- Use `<b>` for section headers and key terms
- Use `<ul>/<ol>` with `<li>` for lists (never dump a wall of text)
- Use `<code>` for commands and technical values
- Use `<br/>` for line breaks within a paragraph
- Keep content structured — avoid single-paragraph dumps of all information

Example of good formatting for Teams:

```bash
READ_WRITE_MODE=1 teams-cli chat send "<chat-id>" --html --body "\
<p>Hi, 查了一下 job 退出原因：</p>
<p><b>异常退出的 Job：</b></p>
<ul>
<li>Job 1104733793 — 03/19 01:19 退出, Exit Code 241</li>
<li>Job 1104806019 — 03/19 01:30 退出, Exit Code 241</li>
</ul>
<p><b>原因：</b>内存超限 (TERM_MEMLIMIT)，请求 6GB，实际用到 8GB 上限被 LSF kill。</p>
<p><b>建议：</b></p>
<ol>
<li>重新 qsub 时加大内存：<code>rusage[mem=16000]</code></li>
<li>修复分辨率：<code>xrandr -s 1920x1080</code></li>
</ol>
<hr/><p><em>Sent via Claude Code (ai-pim-utils)</em></p>"
```

## Safety Guidelines

- **Always confirm with the user** before sending messages or emails on their behalf
- **Show the content** to the user before executing any send/write operation
- **Prefer drafts** over direct sends for email (outlook-cli creates drafts by default)
- The read-only default exists to prevent accidental sends — only enable write mode when the user explicitly requests a write action

## Authentication Prerequisite

All write operations require authentication. Check status first:

```bash
outlook-cli auth status --json
```

If not authenticated, follow the [authenticating-entra-device-code](../authenticating-entra-device-code/SKILL.md) skill.

## Troubleshooting

| Error | Cause | Fix |
|-------|-------|-----|
| `READ_ONLY_MODE` | Write mode not enabled | Set `READ_WRITE_MODE=1` |
| `Permission denied` (exit 6) | Insufficient API scopes | Check with IT for scope approval |
| `Authentication failed` (exit 2) | Token expired | Re-run `outlook-cli auth login` |
| `Not found` (exit 3) | Invalid chat/message ID | Re-list to get correct IDs |
| `Rate limited` (exit 5) | Too many requests | Wait and retry (see `retry_after_seconds`) |

## Related Skills

- [authenticating-entra-device-code](../authenticating-entra-device-code/SKILL.md) — Authentication flow
- [managing-outlook-email](../managing-outlook-email/SKILL.md) — Email read operations
- [managing-calendar](../managing-calendar/SKILL.md) — Calendar read operations
- [managing-teams](../managing-teams/SKILL.md) — Teams read operations
- [managing-confluence](../managing-confluence/SKILL.md) — Confluence read operations
