"""Concierge: slash commands and direct conversation with the crew lead."""

from __future__ import annotations

import hashlib
import os
import re
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .audit import record
from .config import settings
from .db import Budget, ControlFlag, CrewRun, SessionLocal
from . import memory
from .seed import seed_demo
HELP = ("Tell Concierge or Events what you need in ordinary English. For an event, include the "
        "name, date, start and end time, time zone, online link, and number of free spots. "
        "Concierge can also handle office-supply requests and code tasks. "
        "Commands are optional: /crew event, /crew code, /crew status, and /crew budgets. "
        "Ask Concierge 'what happened with my last request?' or use /crew latest for progress.")
FLOW_COMMANDS = {"pantry": "berlin-pantry", "welcome": "welcome-kit", "hoodies": "hoodie-attack"}


def _ids(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


def is_slack_admin(user_id: str) -> bool:
    return bool(user_id) and user_id in _ids("SLACK_ADMIN_USER_IDS")


def is_slack_allowed(user_id: str) -> bool:
    # Empty allowlists grant no access, even in a public or reused workspace.
    return bool(user_id) and (is_slack_admin(user_id) or user_id in _ids("SLACK_ALLOWED_USER_IDS"))


def is_demo_channel(channel_id: str) -> bool:
    configured = os.getenv("SLACK_DEMO_CHANNEL_ID", "")
    return bool(configured) and channel_id == configured


def _submit_run(flow: str, *, source_user: str, channel_id: str,
                delivery_id: str, request_text: str | None = None,
                reply_text: str | None = None, context: str = "",
                thread_root_ts: str | None = None) -> memory.EnqueuedRun:
    return memory.enqueue_delivery_run(flow=flow, user_id=source_user,
                                       channel_id=channel_id, delivery_id=delivery_id,
                                       request_text=request_text, reply_text=reply_text,
                                       context=context, thread_root_ts=thread_root_ts)


def claim_delivery(delivery_id: str) -> bool:
    """Claim a Slack delivery once, before any payment-capable workflow begins."""
    if not delivery_id:
        return False
    # ControlFlag.key is VARCHAR(40) in Postgres.
    key = "slack_delivery_" + hashlib.sha256(delivery_id.encode()).hexdigest()[:24]
    try:
        with SessionLocal.begin() as session:
            session.add(ControlFlag(key=key, value="claimed"))
        return True
    except IntegrityError:
        return False


def queue_allowed_flow(flow: str, *, user_id: str, channel_id: str,
                       delivery_id: str, request_text: str | None = None,
                       context: str = "", thread_root_ts: str | None = None) -> str:
    if not is_slack_allowed(user_id):
        return "This Slack user is not allowed to run crew requests."
    if not is_demo_channel(channel_id):
        return "Crew requests are accepted only in the configured demo channel."
    if flow in {"natural-language", "luma-event", "eventbrite-event", "code-task", "project-plan", "event-status"} and (not request_text or len(request_text) > 1000):
        return "Please include a request of at most 1,000 characters."
    if not delivery_id:
        return "This Slack request was already received, or has no delivery ID. Send a new request if needed."
    if flow in {"eventbrite-event", "luma-event"}:
        lead = "On it. I'll draft the event and show you the details before anything goes live."
    elif flow == "code-task":
        lead = "On it. Buyer will run this in an isolated workspace and bring back the result."
    elif flow == "project-plan":
        lead = "On it. I'll map the request for the crew and show what needs your approval."
    elif flow == "event-status":
        lead = "On it. Events will check the saved RSVP pages and their current status."
    else:
        lead = "On it. I'll bring the right teammates into this thread and keep you posted."
    try:
        queued = _submit_run(flow, source_user=user_id, channel_id=channel_id,
                             delivery_id=delivery_id, request_text=request_text,
                             reply_text=lead, context=context,
                             thread_root_ts=thread_root_ts or memory.active_thread_root())
        if queued.duplicate:
            return queued.cached_reply or "This Slack request was already received. Check its thread for progress."
        return lead
    except Exception:
        # The claim and run queue share one transaction. A previously recorded
        # inbound PENDING turn remains recoverable if that transaction fails.
        with SessionLocal.begin() as session:
            record(session, agent="Concierge", action="slack_queue_failed",
                   detail={"flow": flow, "delivery_key": hashlib.sha256(delivery_id.encode()).hexdigest()},
                   severity="error")
        return "I couldn't start that request. Please check its status before sending it again so we don't create a duplicate."


def status_text(*, budgets: bool) -> str:
    with SessionLocal() as session:
        rows = session.execute(select(Budget).order_by(Budget.office_id, Budget.category)).scalars().all()
        flag = session.get(ControlFlag, "freeze")
        frozen = settings.freeze or bool(flag and flag.value == "true")
        lines = ["Pengwin can take requests here in Slack."]
        lines.append("Spending is paused." if frozen else "Budget checks are on.")
        if settings.payment_mode == "simulated":
            lines.append("Orders use the demo budget; no real card is charged.")
        else:
            lines.append("Payments use Airwallex test funds; they do not move real money.")
        if settings.sandbox_mode == "docker":
            lines.append("Code runs in a separate, disposable workspace.")
        if budgets:
            lines.append("Available budgets:")
            lines.extend(
                f"{'San Francisco' if row.office_id == 'SF' else 'Berlin'} · "
                f"{row.category.replace('-', ' ')}: "
                f"{'$' if row.office_id == 'SF' else '€'}"
                f"{(row.limit_cents - row.spent_cents - row.reserved_cents) / 100:,.2f} left"
                for row in rows
            )
        return "\n".join(lines)


def run_status_text(run_id: str, *, user_id: str) -> str:
    if not is_slack_allowed(user_id):
        return "This Slack user is not allowed to read crew runs."
    try:
        canonical_id = str(UUID(run_id))
    except ValueError:
        return "Ask Concierge what happened with your last request, or use /crew latest."
    from .jobs import get_run

    run = get_run(canonical_id)
    if run is None or (run["source_user"] != user_id and not is_slack_admin(user_id)):
        return "No run is available for this Slack user."
    status_labels = {
        "QUEUED": "I have your request and will start shortly.",
        "RUNNING": "The crew is working on it.",
        "WAITING_APPROVAL": "The event draft is ready for your approval.",
        "COMPLETE": "Done.",
        "HELD": "I paused this because it needs a human check.",
        "FAILED": "I couldn't finish this request.",
        "REJECTED": "You canceled this draft.",
    }
    if run["flow"] == "eventbrite-event":
        if run["status"] == "WAITING_APPROVAL":
            return ("The event draft is ready. Check its Slack thread, then reply there with "
                    "@Pengwin Events approve this event or @Pengwin Events cancel this draft.")
        for job in run["jobs"]:
            if job["kind"] == "eventbrite_publish" and job["output"].get("url"):
                return f"Done. Events published your public RSVP page:\n{job['output']['url']}"
    if run["flow"] == "code-task" and run["status"] == "COMPLETE":
        return "Done. Buyer ran the code. The result is in your request's Slack thread."
    action_labels = {
        "purchase": "the order quote",
        "requote": "the corrected quote",
        "pay": "the budget",
        "code_execute": "the code",
        "lunch_prepare": "the lunch plan",
        "lunch_complete": "the lunch details",
        "eventbrite_publish": "the RSVP page",
        "luma_publish": "the calendar event",
    }
    lines = [status_labels.get(run["status"], "Here's the latest on your request.")]
    for job in run["jobs"][-12:]:
        if job["kind"] == "dispatch":
            continue
        task = action_labels.get(job["kind"], "their part")
        if job["status"] == "DONE":
            lines.append(f"{job['role']} finished {task}.")
        elif job["status"] == "RUNNING":
            lines.append(f"{job['role']} is working on {task}.")
        elif job["status"] == "QUEUED":
            lines.append(f"{job['role']} will handle {task} next.")
        elif job["status"] in {"HELD", "FAILED"}:
            lines.append(f"{job['role']} stopped while handling {task}.")
    if run["status"] in {"HELD", "FAILED"}:
        lines.append("Please check with the crew before sending the same request again.")
    return "\n".join(lines)


def latest_run_status_text(*, user_id: str, channel_id: str) -> str:
    if not is_slack_allowed(user_id) or not is_demo_channel(channel_id):
        return "I can't show requests from outside your Pengwin channel."
    with SessionLocal() as session:
        run_id = session.execute(select(CrewRun.id).where(
            CrewRun.source_user == user_id, CrewRun.channel_id == channel_id,
        ).order_by(CrewRun.created_at.desc(), CrewRun.id.desc()).limit(1)).scalar_one_or_none()
    if run_id is None:
        return "I don't see a recent request from you in this channel yet."
    return run_status_text(run_id, user_id=user_id)


def handle_command(command: dict) -> str:
    user_id = command.get("user_id", "")
    channel_id = command.get("channel_id", "")
    raw = command.get("text", "").strip()
    action = raw.lower()
    if not is_demo_channel(channel_id):
        return "Use this crew app in its configured demo channel."
    if action in FLOW_COMMANDS:
        return queue_allowed_flow(FLOW_COMMANDS[action], user_id=user_id, channel_id=channel_id,
                                  delivery_id=("command:" + command["trigger_id"]) if command.get("trigger_id") else "")
    if action.startswith("run "):
        return run_status_text(action.removeprefix("run ").strip(), user_id=user_id)
    if action == "latest":
        return latest_run_status_text(user_id=user_id, channel_id=channel_id)
    if action.startswith("review "):
        from .jobs import review_luma_run
        return review_luma_run(action.removeprefix("review ").strip(), user_id=user_id)
    if action.startswith("approve "):
        from .jobs import approve_luma_run
        parts = raw.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{12}", parts[2]):
            return "Copy the approval command from the event draft. It includes the request reference and approval code."
        try:
            run_id = str(UUID(parts[1]))
        except ValueError:
            return "Copy the full request reference from the event draft."
        return approve_luma_run(run_id, parts[2], user_id=user_id)
    if action.startswith("reject "):
        from .jobs import reject_luma_run
        try:
            run_id = str(UUID(raw.split(maxsplit=1)[1].strip()))
        except ValueError:
            return "Copy the full request reference from the event draft."
        return reject_luma_run(run_id, user_id=user_id)
    if action in ("status", "budgets"):
        if not is_slack_allowed(user_id):
            return "This Slack user is not allowed to read crew status."
        return status_text(budgets=action == "budgets")
    if action in ("freeze", "unfreeze", "reset-demo"):
        if not is_slack_admin(user_id):
            return "Only a configured crew admin can use that command."
        if action == "reset-demo":
            if settings.payment_mode != "simulated":
                return "Reset is available only in simulated payment mode."
            seed_demo(reset=True)
            return "Local demo state reset."
        frozen = action == "freeze"
        if not frozen and settings.freeze:
            return "FREEZE=true in the environment; clear it before unfreezing."
        with SessionLocal.begin() as session:
            flag = session.get(ControlFlag, "freeze")
            flag.value = "true" if frozen else "false"
            record(session, agent="Control", action="freeze_changed",
                   detail={"frozen": frozen, "source": "slack", "user_id": user_id},
                   severity="warning" if frozen else "info")
        return f"Payments are now {'frozen' if frozen else 'unfrozen'}."
    if action in ("", "help"):
        return HELP
    if action.startswith("event "):
        return queue_allowed_flow("eventbrite-event", user_id=user_id, channel_id=channel_id,
                                  delivery_id=("command:" + command["trigger_id"]) if command.get("trigger_id") else "",
                                  request_text=raw[6:].strip())
    if action.startswith("code "):
        return queue_allowed_flow("code-task", user_id=user_id, channel_id=channel_id,
                                  delivery_id=("command:" + command["trigger_id"]) if command.get("trigger_id") else "",
                                  request_text=raw[5:].strip())
    request_text = raw[6:].strip() if action.startswith("order ") else raw
    return queue_allowed_flow("natural-language", user_id=user_id, channel_id=channel_id,
                              delivery_id=("command:" + command["trigger_id"]) if command.get("trigger_id") else "",
                              request_text=request_text)


def build_app() -> App:
    token = os.getenv("SLACK_CONCIERGE_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_CONCIERGE_BOT_TOKEN is missing")
    if not os.getenv("SLACK_APP_TOKEN"):
        raise RuntimeError("SLACK_APP_TOKEN is missing")
    if not os.getenv("SLACK_DEMO_CHANNEL_ID"):
        raise RuntimeError("SLACK_DEMO_CHANNEL_ID is missing")
    if not (_ids("SLACK_ALLOWED_USER_IDS") or _ids("SLACK_ADMIN_USER_IDS")):
        raise RuntimeError("Set SLACK_ALLOWED_USER_IDS or SLACK_ADMIN_USER_IDS before starting Slack")
    bot = App(token=token)

    @bot.event("app_mention")
    def mention(event, body, say):
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user", "")
        channel_id = event.get("channel", "")
        if not is_demo_channel(channel_id):
            return
        if not is_slack_allowed(user_id):
            say("This Slack user is not allowed to run crew requests.")
            return
        text = re.sub(r"<@[^>]+>", "", event.get("text", "")).strip()
        thread_root_ts = event.get("thread_ts") or event.get("ts")
        delivery_id = ("event:" + body["event_id"] if body.get("event_id") else
                       f"message:{channel_id}:{event.get('ts', '')}")
        claim = memory.record_inbound(role="Concierge", user_id=user_id, channel_id=channel_id,
                                      delivery_id=delivery_id, text=text,
                                      message_ts=event.get("ts"),
                                      thread_root_ts=thread_root_ts)
        if not claim.accepted:
            return
        context = memory.format_recent_turns(memory.recent_turns(
            role="Concierge", user_id=user_id, channel_id=channel_id,
            thread_root_ts=thread_root_ts, exclude_delivery_id=delivery_id))
        if claim.cached_reply:
            reply = claim.cached_reply
        else:
            from .dialogue import answer
            thread_token = memory.set_active_thread_root(thread_root_ts)
            try:
                reply = answer("Concierge", text, user_id=user_id, channel_id=channel_id,
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
            memory.record_outbound(role="Concierge", user_id=user_id, channel_id=channel_id,
                                   delivery_id=delivery_id, reply=reply,
                                   message_ts=str(posted_ts),
                                   thread_root_ts=thread_root_ts)

    @bot.command("/crew")
    def command(ack, client, command):
        ack()  # Slack requires this before slow model/browser work.
        client.chat_postEphemeral(channel=command["channel_id"],
                                  user=command["user_id"],
                                  text=handle_command(command))

    return bot


def main() -> None:
    app = build_app()
    seed_demo()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
