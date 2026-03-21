# src/niuma/teams_api.py
"""Direct Microsoft Graph API calls for Teams operations not supported by teams-cli."""
from __future__ import annotations

import asyncio
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
    """Read a valid access token from the shared ai-pim-utils token cache.

    If no valid (non-expired) access token is found, attempts to use a refresh
    token from the cache to obtain a new access token via the OAuth token endpoint.
    If refresh fails, logs a clear error directing the user to re-authenticate.
    """
    if not _TOKEN_CACHE.exists():
        raise RuntimeError("Token cache not found. Run 'teams-cli auth login' first.")

    with open(_TOKEN_CACHE) as f:
        data = json.load(f)

    now = int(time.time())
    # First pass: look for a valid (non-expired) access token
    for _key, token_entry in data.get("AccessToken", {}).items():
        if int(token_entry.get("expires_on", 0)) > now:
            return token_entry["secret"]

    # No valid access token found — try to refresh using a refresh token
    logger.warning("Access token expired. Attempting to refresh using refresh token...")
    refresh_token = None
    client_id = None
    tenant_id = None

    for _key, rt_entry in data.get("RefreshToken", {}).items():
        rt = rt_entry.get("secret")
        if rt:
            refresh_token = rt
            client_id = rt_entry.get("client_id") or rt_entry.get("clientId")
            break

    # Try to get tenant from Account or token entry metadata
    for _key, acct in data.get("Account", {}).items():
        realm = acct.get("realm")
        if realm and realm != "common":
            tenant_id = realm
            break

    if not refresh_token:
        logger.error(
            "No refresh token found in cache. Run 'teams-cli auth login' to re-authenticate."
        )
        raise RuntimeError(
            "Access token expired and no refresh token available. "
            "Run 'teams-cli auth login' to re-authenticate."
        )

    if not client_id or not tenant_id:
        logger.error(
            "Cannot refresh token: missing client_id or tenant_id in cache. "
            "Run 'teams-cli auth login' to re-authenticate."
        )
        raise RuntimeError(
            "Access token expired. Could not find client_id/tenant_id for refresh. "
            "Run 'teams-cli auth login' to re-authenticate."
        )

    try:
        import urllib.parse
        token_url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
        post_data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": "https://graph.microsoft.com/.default offline_access",
        }).encode()

        req = urllib.request.Request(token_url, data=post_data, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        resp = urllib.request.urlopen(req, timeout=15)
        token_response = json.loads(resp.read())
        new_access_token = token_response.get("access_token")
        if not new_access_token:
            raise RuntimeError("Token refresh response missing access_token")
        logger.info("Successfully refreshed access token via refresh token.")
        return new_access_token
    except Exception as exc:
        logger.error(
            "Token refresh failed: %s. Run 'teams-cli auth login' to re-authenticate.", exc
        )
        raise RuntimeError(
            f"Access token expired and refresh failed: {exc}. "
            "Run 'teams-cli auth login' to re-authenticate."
        ) from exc


def _graph_post_sync(endpoint: str, body: dict[str, Any]) -> dict[str, Any]:
    """Make a POST request to Microsoft Graph API (synchronous, run via asyncio.to_thread)."""
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


def _get_me_sync() -> dict[str, Any]:
    """Fetch current user info from Graph API (synchronous, run via asyncio.to_thread)."""
    token = _get_access_token()
    req = urllib.request.Request("https://graph.microsoft.com/v1.0/me")
    req.add_header("Authorization", f"Bearer {token}")
    return json.loads(urllib.request.urlopen(req).read())


def create_session_chat(
    *,
    session_id: str,
    topic: str,
    user_email: str,
) -> dict[str, str]:
    """Create a dedicated group chat for a niuma session (synchronous).

    Returns dict with 'chat_id' and 'web_url'.
    Call via asyncio.to_thread() from async contexts.
    """
    chat_topic = topic

    # Use 'me' endpoint to get current user's ID for self-chats
    # when user_email might be a displayName instead of email
    me = _get_me_sync()
    user_id = me["id"]

    result = _graph_post_sync("/chats", {
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


async def create_session_chat_async(
    *,
    session_id: str,
    topic: str,
    user_email: str,
) -> dict[str, str]:
    """Async wrapper for create_session_chat using asyncio.to_thread."""
    return await asyncio.to_thread(
        create_session_chat,
        session_id=session_id,
        topic=topic,
        user_email=user_email,
    )


def add_chat_member(*, chat_id: str, user_email: str) -> None:
    """Add a user to an existing group chat via Graph API."""
    token = _get_access_token()
    body = {
        "@odata.type": "#microsoft.graph.aadUserConversationMember",
        "roles": ["owner"],
        "user@odata.bind": f"https://graph.microsoft.com/v1.0/users/{user_email}",
    }
    _graph_post_sync(f"/chats/{chat_id}/members", body)


async def add_chat_member_async(*, chat_id: str, user_email: str) -> None:
    """Async wrapper for add_chat_member."""
    await asyncio.to_thread(add_chat_member, chat_id=chat_id, user_email=user_email)
