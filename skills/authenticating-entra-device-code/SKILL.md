---
name: authenticating-entra-device-code
description: Authenticate with Microsoft Entra ID via device code flow. Shared by outlook-cli, calendar-cli, transcript-cli, dl-cli, onedrive-cli, onenote-cli, and teams-cli. Use when authentication fails, tokens expire, or any Entra-authenticated CLI command hangs or returns auth errors.
---

# Entra Device Code Authentication

All Microsoft Graph and Entra-authenticated CLIs share the same device code auth flow and token cache (`~/.ai-pim-utils/auth-cache`). Authenticating once covers all of them.

## Prerequisites

The following CLIs use this auth flow: `outlook-cli`, `calendar-cli`, `transcript-cli`, `dl-cli`, `onedrive-cli`, `onenote-cli`, `teams-cli`.

Note: `sharepoint-cli`, `gdrive-cli`, and `glean-cli` use MaaS MCP auth (`<cli> auth login`), not Entra device code.

## Pre-Flight Auth Check

**CRITICAL: Always check auth status before running commands.**

Device code auth blocks the process waiting for human action in a browser. If you skip this check, commands may hang with no visible output — the human user will not know the system is waiting for them.

```bash
# Use any Entra-authenticated CLI (they share the same cache)
outlook-cli auth status --json
```

Response when authenticated:
```json
{ "authenticated": true, "username": "user@example.com" }
```

Response when NOT authenticated:
```json
{ "authenticated": false }
```

## Authentication Workflow

If `"authenticated": false`, run the login flow and surface the result to the human:

```bash
outlook-cli auth login
```

This displays a formatted prompt with the URL and code:
```
╔══════════════════════════════════════════════════════════════╗
║                                                              ║
║  Microsoft Authentication Required                           ║
║                                                              ║
║  1. Open:  https://microsoft.com/devicelogin                ║
║  2. Enter: XXXXXXXX                                         ║
║                                                              ║
║  Waiting for authentication...                               ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
  Code copied to clipboard.
```

**Immediately tell the user:**

> **Action needed:** Open https://microsoft.com/devicelogin in your browser and enter the code shown above (it may already be copied to your clipboard). Let me know when you've completed the login.

The `auth login` process blocks until the human completes authentication. Wait for it to finish before running any other commands. Any Entra-authenticated CLI can be used for login — the token cache is shared.

## Common Workflows

### First-time setup
1. Run `<cli> auth status --json` — expect `"authenticated": false`
2. Run `<cli> auth login` — surface the URL and code to the human
3. Wait for login to complete
4. Proceed with commands

### Token expired mid-session
If a command fails with an auth error after previously working:
1. Run `<cli> auth status --json` to confirm
2. Run `<cli> auth login` to re-authenticate
3. Surface the URL and code to the human
4. Retry the failed command after login completes

### Cache corruption
If auth commands themselves error out:
```bash
rm ~/.ai-pim-utils/auth-cache
outlook-cli auth login
```

## Troubleshooting

**Command hangs with no output:** The CLI is likely waiting for device code authentication. Run `auth status --json` in another invocation to check, then cancel the hung command and follow the authentication workflow above.

**"authenticated: true" but commands still fail:** The cached token may have expired silently. Run `auth login` to refresh credentials.
