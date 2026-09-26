"""Post a role's verified backend update as that role's Slack bot identity."""

from __future__ import annotations

import os

from slack_sdk import WebClient


ROLE_TOKEN_ENV = {
    "Concierge": "SLACK_CONCIERGE_BOT_TOKEN",
    "Buyer": "SLACK_BUYER_BOT_TOKEN",
    "Events": "SLACK_EVENTS_BOT_TOKEN",
    "Treasurer": "SLACK_TREASURER_BOT_TOKEN",
}


def post_role_update(role: str, channel_id: str, text: str, *, thread_ts: str | None = None) -> str:
    """Return the Slack message timestamp; never accept a channel outside the demo."""
    if role not in ROLE_TOKEN_ENV:
        raise ValueError("Unknown crew role")
    allowed_channel = os.getenv("SLACK_DEMO_CHANNEL_ID", "")
    if not allowed_channel or channel_id != allowed_channel:
        raise ValueError("Role update destination is not the configured demo channel")
    token = os.getenv(ROLE_TOKEN_ENV[role], "")
    if not token:
        raise RuntimeError(f"{ROLE_TOKEN_ENV[role]} is missing")
    if not text or len(text) > 3000:
        raise ValueError("Role update must contain 1–3000 characters")
    # Treat model/vendor text as plain data. Slack control sequences and mentions
    # must not be allowed to turn a backend update into an unexpected notification.
    safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    message = {
        "channel": channel_id,
        "text": safe_text,
        "mrkdwn": False,
        "link_names": False,
        "unfurl_links": False,
        "unfurl_media": False,
    }
    if thread_ts:
        message["thread_ts"] = thread_ts
    response = WebClient(token=token).chat_postMessage(**message)
    if not response.get("ok") or not response.get("ts"):
        raise RuntimeError("Slack did not confirm the role update")
    return str(response["ts"])
