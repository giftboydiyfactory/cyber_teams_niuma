# src/niuma/teams_api.py
"""Direct Microsoft Graph API calls for Teams operations not supported by teams-cli."""
from __future__ import annotations

import json
import logging
import urllib.request
import urllib.error
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TOKEN_CACHE = Path.home() / ".ai-pim-utils" / "token-cache-ai-pim-utils"


def _get_access_token() -> str:
    """Read a valid access token from the shared ai-pim-utils token cache."""
    if not _TOKEN_CACHE.exists():
        raise RuntimeError("Token cache not found. Run 'teams-cli auth login' first.")

    with open(_TOKEN_CACHE) as f:
        data = json.load(f)

    now = int(time.time())
    for _key, token_entry in data.get("AccessToken", {}).items():
        if int(token_entry.get("expires_on", 0)) > now:
            return token_entry["secret"]

    raise RuntimeError("No valid access token found. Run 'teams-cli auth login' to refresh.")


def _graph_post(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """Make a POST request to Microsoft Graph API."""
    token = _get_access_token()
    url = f"https://graph.microsoft.com/v1.0{endpoint}"
    data = json.dumps(body).encode()

    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")

    try:
        resp = urllib.request.urlopen(req)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()[:500]
        logger.error("Graph API error %d: %s", e.code, error_body)
        raise RuntimeError(f"Graph API error {e.code}: {error_body}")


def create_session_chat(
    *,
    session_id: str,
    topic: str,
    user_email: str,
) -> dict[str, str]:
    """Create a dedicated group chat for a niuma session.

    Returns dict with 'chat_id' and 'web_url'.
    """
    chat_topic = f"niuma [{session_id}] {topic[:50]}"

    # Use 'me' endpoint to get current user's ID for self-chats
    # when user_email might be a displayName instead of email
    token = _get_access_token()
    req = urllib.request.Request("https://graph.microsoft.com/v1.0/me")
    req.add_header("Authorization", f"Bearer {token}")
    me = json.loads(urllib.request.urlopen(req).read())
    user_id = me["id"]

    result = _graph_post("/chats", {
        "chatType": "group",
        "topic": chat_topic,
        "members": [
            {
                "@odata.type": "#microsoft.graph.aadUserConversationMember",
                "roles": ["owner"],
                "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user_id}",
            }
        ],
    })

    return {
        "chat_id": result["id"],
        "web_url": result.get("webUrl", ""),
        "topic": chat_topic,
    }
