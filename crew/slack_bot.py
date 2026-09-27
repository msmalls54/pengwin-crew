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
from .db import Budget, ControlFlag, SessionLocal
from .seed import seed_demo
HELP = ("Mention Concierge with a plain-English request, such as 'please order 3 oat milk cartons "
        "and 2 coffee bags for Berlin,' or use `/crew order ...`. Include quantities and the office. "
        "For a public RSVP event, use `/crew event <name, date, time, place/link, capacity>` or mention Events. "
        "For a disposable Python task, use `/crew code <plain-English goal>`. "
        "The demo shortcuts are `/crew pantry`, `/crew welcome`, and `/crew hoodies`. "
        "Use `/crew run <id>`, `/crew review <id>`, `/crew status`, or `/crew budgets` to check progress.")
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
                request_text: str | None = None) -> str:
    from .jobs import submit_run

    if request_text is None:
        return submit_run(flow, source_user=source_user, channel_id=channel_id)
    return submit_run(flow, source_user=source_user, channel_id=channel_id,
                      request_text=request_text)


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
                       delivery_id: str, request_text: str | None = None) -> str:
    if not is_slack_allowed(user_id):
        return "This Slack user is not allowed to run crew requests."
    if not is_demo_channel(channel_id):
        return "Crew requests are accepted only in the configured demo channel."
    if flow in {"natural-language", "luma-event", "eventbrite-event", "code-task"} and (not request_text or len(request_text) > 1000):
        return "Please include a request of at most 1,000 characters."
    if not claim_delivery(delivery_id):
        return "This Slack request was already received, or has no delivery ID. Send a new request if needed."
    try:
        run_id = _submit_run(flow, source_user=user_id, channel_id=channel_id,
                             request_text=request_text)
        from .jobs import flow_label
        return (f"On it — I've queued {flow_label(flow)} as run {run_id}. "
                "I'll bring the crew into this channel as the work moves along.")
    except Exception:
        # Preserve the claim: a retry must not create a second purchase-capable job.
        with SessionLocal.begin() as session:
            record(session, agent="Concierge", action="slack_queue_failed",
                   detail={"flow": flow, "delivery_key": hashlib.sha256(delivery_id.encode()).hexdigest()},
                   severity="error")
        return "The request could not be queued. Check the crew audit timeline before sending a new request."


def status_text(*, budgets: bool) -> str:
    with SessionLocal() as session:
        rows = session.execute(select(Budget).order_by(Budget.office_id, Budget.category)).scalars().all()
        flag = session.get(ControlFlag, "freeze")
        frozen = settings.freeze or bool(flag and flag.value == "true")
        lines = [f"Payments: {'FROZEN' if frozen else 'active'}",
                 f"Planner: {settings.planner_mode}" +
                 (f" ({settings.vultr_model})" if settings.planner_mode == "vultr" else ""),
                 f"Browser: {settings.sandbox_mode}; payments: {settings.payment_mode}"]
        if budgets:
            lines.extend(
                f"{row.office_id} {row.category}: "
                f"{(row.limit_cents - row.spent_cents - row.reserved_cents) / 100:.2f} "
                f"{'USD' if row.office_id == 'SF' else 'EUR'} remaining"
                for row in rows
            )
        return "\n".join(lines)


def run_status_text(run_id: str, *, user_id: str) -> str:
    if not is_slack_allowed(user_id):
        return "This Slack user is not allowed to read crew runs."
    try:
        canonical_id = str(UUID(run_id))
    except ValueError:
        return "Use a full run ID from Concierge's reply."
    from .jobs import get_run

    run = get_run(canonical_id)
    if run is None or (run["source_user"] != user_id and not is_slack_admin(user_id)):
        return "No run is available for this Slack user."
    lines = [f"Run {run['id']}: {run['status']} ({run['flow']})."]
    for job in run["jobs"][-12:]:
        line = f"{job['role']} {job['kind']}: {job['status']}"
        if job["role"] == "Treasurer" and job["output"].get("payment_status"):
            line += f" / {job['output']['payment_status']}"
            if job["output"].get("blocked_rule"):
                line += f" ({job['output']['blocked_rule']})"
        lines.append(line)
    if run["status"] in {"HELD", "FAILED"}:
        lines.append("Operator review is required; do not repeat a payment request blindly.")
    return "\n".join(lines)


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
    if action.startswith("review "):
        from .jobs import review_luma_run
        return review_luma_run(action.removeprefix("review ").strip(), user_id=user_id)
    if action.startswith("approve "):
        from .jobs import approve_luma_run
        parts = raw.split()
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{12}", parts[2]):
            return "Use /crew approve <run ID> <12-character draft snapshot>."
        try:
            run_id = str(UUID(parts[1]))
        except ValueError:
            return "Use the full run ID from the event draft."
        return approve_luma_run(run_id, parts[2], user_id=user_id)
    if action.startswith("reject "):
        from .jobs import reject_luma_run
        try:
            run_id = str(UUID(raw.split(maxsplit=1)[1].strip()))
        except ValueError:
            return "Use the full run ID from the event draft."
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
        from .dialogue import answer
        say(answer("Concierge", text, user_id=user_id, channel_id=channel_id,
                   delivery_id=("event:" + body["event_id"]) if body.get("event_id") else ""))

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
