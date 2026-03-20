---
name: managing-teams
description: "Manage Microsoft Teams chats, channels, and group chats via teams-cli and Graph API. Create group chats, send messages, manage members, update topics, browse teams/channels. Use when working with Teams messages, creating chats, or managing Teams interactions."
---

# Teams Chat & Channel Management

Manage Microsoft Teams chats and channels via `teams-cli` and direct Graph API calls.

## Status: Active

`teams-cli` v0.46.2 is installed and authenticated.

## Verify Installation

```bash
teams-cli --version
teams-cli auth status --json
```

If command not found, see [installation page](https://outlook-cli-80d21a.gitlab-master-pages.nvidia.com/).

## teams-cli Commands

### Chats

```bash
# List chats (1:1, group, meeting)
teams-cli chat list --json

# Get chat details
teams-cli chat get "<chat-id>" --json

# Read messages from a chat
teams-cli chat read "<chat-id>" --limit 20 --json

# Send message to a chat (requires READ_WRITE_MODE=1)
READ_WRITE_MODE=1 teams-cli chat send "<chat-id>" --body "Hello!"

# Send HTML message (recommended for formatting)
READ_WRITE_MODE=1 teams-cli chat send "<chat-id>" --html --body "<b>Bold</b>"

# Open chat in Teams app
teams-cli chat open "<chat-id>"
```

### Teams & Channels

```bash
# List joined teams
teams-cli team list --json

# List channels in a team
teams-cli channel list <team-id> --json

# Read channel messages
teams-cli channel read <team-id> <channel-id> --limit 25 --json

# Open channel in Teams app
teams-cli channel open <team-id> <channel-id>
```

## Graph API Direct Calls (via token)

Token location: `~/.ai-pim-utils/token-cache-ai-pim-utils`

### Available Graph API Capabilities

| Operation | Endpoint | Method | Scope | Status |
|-----------|----------|--------|-------|--------|
| Create group chat | `/chats` | POST | Chat.ReadWrite | ✅ |
| Update chat topic | `/chats/{id}` | PATCH | Chat.ReadWrite | ✅ |
| Get chat members | `/chats/{id}/members` | GET | ChatMember.ReadWrite | ✅ |
| Add member to chat | `/chats/{id}/members` | POST | ChatMember.ReadWrite | ✅ |
| Remove member | `/chats/{id}/members/{id}` | DELETE | ChatMember.ReadWrite | ✅ |
| Get user profile | `/me` | GET | User.Read | ✅ |
| List all chats | `/me/chats` | GET | Chat.ReadWrite | ✅ |
| Send email | `/me/sendMail` | POST | Mail.Send | ✅ |
| Read/write files | `/me/drive` | GET/PUT | Files.ReadWrite.All | ✅ |
| Read OneNote | `/me/onenote` | GET | Notes.ReadWrite | ✅ |
| Send channel message | `/teams/{id}/channels/{id}/messages` | POST | ChannelMessage.Send | ❌ Missing scope |
| Create channel | `/teams/{id}/channels` | POST | Channel.Create | ❌ Missing scope |
| Reply to chat message | `/chats/{id}/messages/{id}/replies` | POST | N/A | ❌ API not supported |

### Graph API Examples

**Create a group chat:**
```python
import json, urllib.request

token = _get_access_token()  # from token cache
me = _get_user_id(token)     # GET /me

body = json.dumps({
    "chatType": "group",
    "topic": "My Chat Title",
    "members": [{
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{me['id']}",
    }]
}).encode()

req = urllib.request.Request(
    "https://graph.microsoft.com/v1.0/chats",
    data=body, method="POST"
)
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Content-Type", "application/json")
result = json.loads(urllib.request.urlopen(req).read())
# result["id"] = chat ID, result["webUrl"] = Teams link
```

**Update chat topic:**
```python
req = urllib.request.Request(
    f"https://graph.microsoft.com/v1.0/chats/{chat_id}",
    data=json.dumps({"topic": "New Title"}).encode(),
    method="PATCH"
)
req.add_header("Authorization", f"Bearer {token}")
req.add_header("Content-Type", "application/json")
urllib.request.urlopen(req)
```

**Add member to chat:**
```python
body = json.dumps({
    "@odata.type": "#microsoft.graph.aadUserConversationMember",
    "roles": ["owner"],
    "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user_email}",
}).encode()
req = urllib.request.Request(
    f"https://graph.microsoft.com/v1.0/chats/{chat_id}/members",
    data=body, method="POST"
)
```

## All Available Graph Scopes

```
Calendars.Read.Shared, Calendars.ReadWrite, CallAiInsights.Read.All,
Channel.ReadBasic.All, ChannelMessage.Read.All, ChannelMessage.ReadWrite,
Chat.ReadWrite, ChatMember.ReadWrite, Files.Read, Files.Read.All,
Files.ReadWrite, Files.ReadWrite.All, GroupMember.Read.All,
Mail.ReadWrite, Mail.Send, MailboxSettings.Read, MailboxSettings.ReadWrite,
Notes.ReadWrite, OnlineMeetingTranscript.Read.All, OnlineMeetings.Read,
OnlineMeetings.ReadWrite, Sites.Read.All, Sites.ReadWrite.All,
Team.ReadBasic.All, User.Read, User.Read.All
```

## Limitations

- **Channel write**: Cannot send to channels or create channels (missing `ChannelMessage.Send` and `Channel.Create` scopes)
- **Chat thread reply**: Graph API `/chats/{id}/messages/{id}/replies` returns 404 — Teams chats don't support threaded replies via API
- **Write mode**: All send operations via teams-cli require `READ_WRITE_MODE=1`

## Output Formats

- `--json` — machine-readable JSON (`{"success": true, "data": {...}, "metadata": {...}}`)
- `--toon` — token-efficient format for LLMs
- Default — human-readable with markdown conversion

## Timestamp Flags

`--relative`, `--utc`, `--local`, `--absolute`, `--timezone <tz>`

## Message Formatting

- Use `--html` flag for formatted messages (plain text loses all formatting in Teams)
- Always append AI attribution: `Sent via Claude Code (ai-pim-utils)`
- Use `<p>`, `<b>`, `<ul>/<li>`, `<code>`, `<table>`, `<br/>` for structure

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 2 | Authentication failed — re-run any command without `--json` to re-auth |
| 3 | Not found |
| 5 | Rate limited — wait `retry_after_seconds`, then retry |
| 6 | Permission denied |
| 11 | Read-only mode — set `READ_WRITE_MODE=1` |

## Related

- [ai-pim-utils-guide](../ai-pim-utils-guide/SKILL.md) for write mode details
- [authenticating-entra-device-code](../authenticating-entra-device-code/SKILL.md) for auth setup
