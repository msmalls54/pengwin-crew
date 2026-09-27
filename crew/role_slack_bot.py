"""Inbound Socket Mode listener for Buyer, Events, or Treasurer."""

from __future__ import annotations

import os
import re

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .dialogue import answer
from . import memory
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
        thread_root_ts = event.get("thread_ts") or event.get("ts")
        delivery_id = ("event:" + body["event_id"] if body.get("event_id") else
                       f"message:{channel_id}:{event.get('ts', '')}")
        claim = memory.record_inbound(role=role, user_id=user_id, channel_id=channel_id,
                                      delivery_id=delivery_id, text=text,
                                      message_ts=event.get("ts"),
                                      thread_root_ts=thread_root_ts)
        if not claim.accepted:
            return
        recent = memory.format_recent_turns(memory.recent_turns(
            role=role, user_id=user_id, channel_id=channel_id,
            thread_root_ts=thread_root_ts, exclude_delivery_id=delivery_id))
        project_facts = memory.latest_project_facts(
            user_id=user_id, channel_id=channel_id, thread_root_ts=thread_root_ts)
        context = memory.combine_context(
            recent, memory.format_project_facts(project_facts, for_model=True))
        if claim.cached_reply:
            reply = claim.cached_reply
        elif memory.is_project_recall_request(text):
            reply = memory.format_project_facts(project_facts)
        else:
            thread_token = memory.set_active_thread_root(thread_root_ts)
            try:
                reply = answer(role, text, user_id=user_id, channel_id=channel_id,
                               delivery_id=delivery_id, thread_ts=event.get("thread_ts"),
                               context=context)
            finally:
                memory.reset_active_thread_root(thread_token)
        memory.cache_reply(delivery_id=delivery_id, user_id=user_id,
                           channel_id=channel_id, reply=reply)
        options = {"text": reply}
        if thread_root_ts:
            options["thread_ts"] = thread_root_ts
        sent = say(**options)
        posted_ts = sent.get("ts") if sent is not None else None
        if posted_ts:
            memory.record_outbound(role=role, user_id=user_id, channel_id=channel_id,
                                   delivery_id=delivery_id, reply=reply,
                                   message_ts=str(posted_ts),
                                   thread_root_ts=thread_root_ts)

    return bot


def main() -> None:
    role = os.getenv("ROLE", "")
    app = build_app(role)
    seed_demo()
    SocketModeHandler(app, os.environ[f"SLACK_{role.upper()}_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
