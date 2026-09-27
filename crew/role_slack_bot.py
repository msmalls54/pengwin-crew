"""Inbound Socket Mode listener for Buyer, Events, or Treasurer."""

from __future__ import annotations

import os
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .dialogue import answer
from .seed import seed_demo
from .slack_bot import is_demo_channel, is_slack_allowed


ROLES = {"Buyer", "Events", "Treasurer"}


def build_app(role: str) -> App:
    if role not in ROLES:
        raise ValueError("ROLE must be Buyer, Events, or Treasurer")
    bot_token = os.getenv(f"SLACK_{role.upper()}_BOT_TOKEN")
    app_token = os.getenv(f"SLACK_{role.upper()}_APP_TOKEN")
    if not bot_token or not app_token:
        raise RuntimeError(f"Pengwin {role} needs its bot and app tokens")
    if not os.getenv("SLACK_DEMO_CHANNEL_ID"):
        raise RuntimeError("SLACK_DEMO_CHANNEL_ID is missing")
    if not (os.getenv("SLACK_ALLOWED_USER_IDS") or os.getenv("SLACK_ADMIN_USER_IDS")):
        raise RuntimeError("An allowed Slack user is required")
    bot = App(token=bot_token)

    @bot.event("app_mention")
    def mention(event, body, say):
        if event.get("bot_id") or event.get("subtype"):
            return
        channel_id = event.get("channel", "")
        user_id = event.get("user", "")
        if not is_demo_channel(channel_id):
            return
        if not is_slack_allowed(user_id):
            say("This Slack user is not allowed to run Pengwin requests.")
            return
        text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
        delivery_id = "event:" + body.get("event_id", "") if body.get("event_id") else ""
        reply = answer(role, text, user_id=user_id, channel_id=channel_id,
                       delivery_id=delivery_id, thread_ts=event.get("thread_ts"))
        options = {"text": reply}
        if event.get("thread_ts"):
            options["thread_ts"] = event["thread_ts"]
        say(**options)

    return bot


def main() -> None:
    role = os.getenv("ROLE", "")
    app = build_app(role)
    seed_demo()
    SocketModeHandler(app, os.environ[f"SLACK_{role.upper()}_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
