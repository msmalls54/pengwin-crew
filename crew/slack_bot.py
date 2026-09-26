"""Small, allowlisted Slack Socket Mode front door for the fictional demo."""

from __future__ import annotations

import hashlib
import os

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .audit import record
from .config import settings
from .db import Budget, ControlFlag, SessionLocal
from .seed import seed_demo
from .workflow import run_flow


HELP = "Use `/crew pantry`, `/crew welcome`, `/crew hoodies`, `/crew status`, or `/crew budgets`. Admins can also use `freeze`, `unfreeze`, and `reset-demo`."
FLOW_COMMANDS = {"pantry": "berlin-pantry", "welcome": "welcome-kit", "hoodies": "hoodie-attack"}


def _ids(name: str) -> set[str]:
    return {part.strip() for part in os.getenv(name, "").split(",") if part.strip()}


def is_slack_admin(user_id: str) -> bool:
    return bool(user_id) and user_id in _ids("SLACK_ADMIN_USER_IDS")


def is_slack_allowed(user_id: str) -> bool:
    # Empty allowlists grant no access, even in a public or reused workspace.
    return bool(user_id) and (is_slack_admin(user_id) or user_id in _ids("SLACK_ALLOWED_USER_IDS"))


def mention_intent(text: str) -> str | None:
    lowered = text.lower()
    if "hoodie" in lowered:
        return "hoodie-attack"
    if "new hire" in lowered or "welcome" in lowered:
        return "welcome-kit"
    if "oat milk" in lowered or "coffee" in lowered or "pantry" in lowered:
        return "berlin-pantry"
    return None


def summary(results: list[dict]) -> str:
    lines = []
    for result in results:
        if result["first_status"] == "BLOCKED":
            lines.append(f"Blocked a proposed order ({result['blocked_rule']}).")
        final_status = result.get("corrected_status", result["first_status"])
        lines.append(f"Request {result['request_id'][:8]}: {final_status}.")
    return "\n".join(lines)


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


def run_allowed_flow(flow: str, *, user_id: str, delivery_id: str) -> str:
    if not is_slack_allowed(user_id):
        return "This Slack user is not allowed to run crew requests."
    if not claim_delivery(delivery_id):
        return "This Slack request was already received, or has no delivery ID. Send a new request if needed."
    try:
        results = run_flow(flow, source="slack", source_user=user_id,
                           is_admin=is_slack_admin(user_id))
        return summary(results)
    except Exception:
        # The same delivery must not be replayed after an uncertain provider result.
        with SessionLocal.begin() as session:
            record(session, agent="Concierge", action="slack_flow_failed",
                   detail={"flow": flow, "delivery_key": hashlib.sha256(delivery_id.encode()).hexdigest()},
                   severity="error")
        return "The request could not be completed. Check the crew audit timeline for the last confirmed step. Send a new request only after checking for a payment submission."


def status_text(*, budgets: bool) -> str:
    with SessionLocal() as session:
        rows = session.execute(select(Budget).order_by(Budget.office_id, Budget.category)).scalars().all()
        flag = session.get(ControlFlag, "freeze")
        frozen = settings.freeze or bool(flag and flag.value == "true")
        lines = [f"Payments: {'FROZEN' if frozen else 'active'}"]
        if budgets:
            lines.extend(
                f"{row.office_id} {row.category}: "
                f"{(row.limit_cents - row.spent_cents - row.reserved_cents) / 100:.2f} "
                f"{'USD' if row.office_id == 'SF' else 'EUR'} remaining"
                for row in rows
            )
        return "\n".join(lines)


def handle_command(command: dict) -> str:
    user_id = command.get("user_id", "")
    action = command.get("text", "").strip().lower()
    if action in FLOW_COMMANDS:
        return run_allowed_flow(FLOW_COMMANDS[action], user_id=user_id,
                                delivery_id=("command:" + command["trigger_id"]) if command.get("trigger_id") else "")
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
    return HELP


def build_app() -> App:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is missing")
    if not os.getenv("SLACK_APP_TOKEN"):
        raise RuntimeError("SLACK_APP_TOKEN is missing")
    if not (_ids("SLACK_ALLOWED_USER_IDS") or _ids("SLACK_ADMIN_USER_IDS")):
        raise RuntimeError("Set SLACK_ALLOWED_USER_IDS or SLACK_ADMIN_USER_IDS before starting Slack")
    bot = App(token=token)

    @bot.event("app_mention")
    def mention(event, body, say):
        if event.get("bot_id") or event.get("subtype"):
            return
        user_id = event.get("user", "")
        if not is_slack_allowed(user_id):
            say("This Slack user is not allowed to run crew requests.")
            return
        flow = mention_intent(event.get("text", ""))
        if flow is None:
            say(HELP)
            return
        say(run_allowed_flow(flow, user_id=user_id,
                             delivery_id=("event:" + body["event_id"]) if body.get("event_id") else ""))

    @bot.command("/crew")
    def command(ack, respond, command):
        ack()  # Slack requires this before slow model/browser work.
        respond(handle_command(command))

    return bot


def main() -> None:
    app = build_app()
    seed_demo()
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()


if __name__ == "__main__":
    main()
