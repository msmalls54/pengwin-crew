"""Slack Socket Mode front door. Incoming text is mapped to fixed demo intents."""

import os

from sqlalchemy import select
from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler

from .db import Budget, ControlFlag, SessionLocal
from .seed import seed_demo
from .workflow import run_flow


def intent(text: str) -> str | None:
    lowered = text.lower()
    if "hoodie" in lowered:
        return "hoodie-attack"
    if "new hire" in lowered or "welcome" in lowered:
        return "welcome-kit"
    if "oat milk" in lowered or "coffee" in lowered:
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


def is_slack_admin(user_id: str) -> bool:
    allowed = {part.strip() for part in os.getenv("SLACK_ADMIN_USER_IDS", "").split(",") if part.strip()}
    return user_id in allowed


def build_app() -> App:
    token = os.getenv("SLACK_BOT_TOKEN")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is missing")
    bot = App(token=token)

    @bot.event("app_mention")
    def mention(event, say):
        flow = intent(event.get("text", ""))
        if flow is None:
            say("I can handle Berlin pantry supplies, a Berlin new-hire kit, or crew hoodies. Tell me which one.")
            return
        try:
            results = run_flow(flow, source="slack", source_user="slack-member",
                               is_admin=is_slack_admin(event.get("user", "")))
            say(summary(results))
        except Exception:
            say("The request could not be completed. Check the crew audit timeline for the last confirmed step.")

    @bot.command("/crew")
    def command(ack, respond, command):
        ack()
        user_id = command.get("user_id", "")
        action = command.get("text", "").strip().lower()
        if action in ("freeze", "unfreeze", "reset-demo") and not is_slack_admin(user_id):
            respond("Only a configured crew admin can use that command.")
            return
        if action in ("freeze", "unfreeze"):
            with SessionLocal.begin() as session:
                session.get(ControlFlag, "freeze").value = "true" if action == "freeze" else "false"
            respond(f"Payments are now {'frozen' if action == 'freeze' else 'unfrozen'}.")
        elif action == "reset-demo":
            if os.getenv("PAYMENT_MODE", "simulated") != "simulated":
                respond("Reset is available only in simulated payment mode.")
                return
            seed_demo(reset=True)
            respond("Local demo state reset.")
        elif action in ("budgets", "status"):
            with SessionLocal() as session:
                budgets = session.execute(select(Budget).order_by(Budget.office_id, Budget.category)).scalars().all()
                frozen = session.get(ControlFlag, "freeze").value == "true"
            lines = [f"Payments: {'FROZEN' if frozen else 'active'}"]
            if action == "budgets":
                lines.extend(f"{b.office_id} {b.category}: {(b.limit_cents-b.spent_cents-b.reserved_cents)/100:.2f} remaining" for b in budgets)
            respond("\n".join(lines))
        else:
            respond("Use `/crew status`, `/crew budgets`, `/crew freeze`, `/crew unfreeze`, or `/crew reset-demo`.")
    return bot


def main() -> None:
    seed_demo()
    app = build_app()
    app_token = os.getenv("SLACK_APP_TOKEN")
    if not app_token:
        raise RuntimeError("SLACK_APP_TOKEN is missing")
    SocketModeHandler(app, app_token).start()


if __name__ == "__main__":
    main()
