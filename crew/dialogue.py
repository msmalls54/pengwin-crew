"""Bounded, plain-English Slack conversations for the four Pengwin identities."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .db import Payment, SessionLocal
from .inference import VultrInference
from .slack_bot import FLOW_COMMANDS, queue_allowed_flow, run_status_text, status_text


def _period(text: str, now: datetime) -> tuple[str, datetime | None, datetime | None]:
    local = now.astimezone(ZoneInfo(os.getenv("SLACK_REPORT_TIMEZONE", "America/Los_Angeles")))
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    lower = text.lower()
    if "yesterday" in lower:
        return "yesterday", start - timedelta(days=1), start
    if "this week" in lower or "week so far" in lower:
        return "this week", start - timedelta(days=start.weekday()), None
    if "this month" in lower or "month so far" in lower:
        return "this month", start.replace(day=1), None
    if "today" in lower or "so far" in lower:
        return "today", start, start + timedelta(days=1)
    return "in the Pengwin ledger", None, None


def _money(cents: int, currency: str) -> str:
    symbol = {"EUR": "€", "USD": "$"}.get(currency)
    return f"{symbol}{cents / 100:,.2f}" if symbol else f"{cents / 100:,.2f} {currency}"


def spend_report(text: str, *, now: datetime | None = None) -> str:
    """Report recorded simulation and sandbox submissions separately.

    No ledger entry in this deployment proves real-world settlement.
    """
    now = now or datetime.now(timezone.utc)
    label, start, end = _period(text, now)
    stmt = select(Payment.status, Payment.currency, func.sum(Payment.amount_cents), func.count()).where(
        Payment.status.in_(["SIMULATED", "SUBMITTED_SANDBOX"])
    )
    if start is not None:
        stmt = stmt.where(Payment.created_at >= start.astimezone(timezone.utc))
    if end is not None:
        stmt = stmt.where(Payment.created_at < end.astimezone(timezone.utc))
    stmt = stmt.group_by(Payment.status, Payment.currency).order_by(Payment.status, Payment.currency)
    with SessionLocal() as session:
        rows = session.execute(stmt).all()
    if not rows:
        return f"Ledger {label}: no demo checkouts or sandbox transfer submissions recorded."
    parts = []
    for status, currency, total, count in rows:
        description = "demo checkout" if status == "SIMULATED" else "sandbox transfer submission (unsettled)"
        parts.append(f"{_money(int(total), currency)} in {count} {description}{'' if count == 1 else 's'}")
    return f"Ledger {label}: " + "; ".join(parts) + "."


def _looks_like_purchase(text: str) -> bool:
    if re.search(r"\b(order|buy|purchase|replenish|restock|pick up|shop for)\b", text, re.I):
        return True
    return bool(re.search(r"\b(need|want|get)\b", text, re.I) and
                re.search(r"\b\d+\b", text) and
                re.search(r"\b(oat milk|coffee|hoodie|welcome kit|supplies)\b", text, re.I))


def _looks_like_event_request(text: str) -> bool:
    return bool(re.search(r"\b(set up|schedule|organize|plan|create|book|arrange|invite)\b", text, re.I) and
                re.search(r"\b(event|lunch|meeting|party|gathering|invite)\b", text, re.I))


def answer(role: str, text: str, *, user_id: str, channel_id: str, delivery_id: str) -> str:
    """Only established workflows may mutate outside Slack; chat itself is read-only."""
    if role not in {"Concierge", "Buyer", "Events", "Treasurer"}:
        raise ValueError("Unknown crew role")
    text = text.strip()
    if not text:
        return f"I'm Pengwin {role}. Tell me what you need in plain English."
    if len(text) > 1000:
        return "Keep the request under 1,000 characters so I can handle it cleanly."
    lower = text.lower()
    if role == "Treasurer":
        if re.search(r"\b(spent|spend|paid|payments|charges|outflow)\b", lower):
            return spend_report(text)
        if re.search(r"\b(budget|remaining|left to spend)\b", lower):
            return "Here are the demo budget balances:\n" + status_text(budgets=True)
        if re.search(r"\b(send|transfer|pay|reimburse|refund)\b", lower):
            return ("I can review the ledger and budget, but I won't move money from a chat message. "
                    "Have Concierge queue a supported request for human review; live payments are disabled.")
    if lower.startswith("run "):
        return run_status_text(text[4:].strip(), user_id=user_id)
    if role == "Concierge":
        if lower in FLOW_COMMANDS:
            return queue_allowed_flow(FLOW_COMMANDS[lower], user_id=user_id, channel_id=channel_id,
                                      delivery_id=delivery_id)
        if lower in {"status", "crew status"}:
            return status_text(budgets=False)
        if lower in {"budgets", "budget"}:
            return status_text(budgets=True)
    if role in {"Concierge", "Buyer"} and _looks_like_purchase(text):
        return queue_allowed_flow("natural-language", user_id=user_id, channel_id=channel_id,
                                  delivery_id=delivery_id, request_text=text)
    if role in {"Concierge", "Events"} and _looks_like_event_request(text):
        flow = "luma-event" if re.search(r"\b(?:luma|lu\.ma)\b", text, re.I) else "eventbrite-event"
        return queue_allowed_flow(flow, user_id=user_id, channel_id=channel_id,
                                  delivery_id=delivery_id, request_text=text)
    if role == "Concierge" and re.search(r"\b(calculate|compute|analy[sz]e|run python|run code)\b", text, re.I):
        return queue_allowed_flow("code-task", user_id=user_id, channel_id=channel_id,
                                  delivery_id=delivery_id, request_text=text)
    facts = ""
    if role == "Treasurer":
        facts = spend_report("today") + "\n" + status_text(budgets=True)
    try:
        return VultrInference().role_reply(role=role, request_text=text, facts=facts)
    except Exception:
        # A model outage should not pretend a task or factual answer succeeded.
        return (f"Pengwin {role} is here, but my reasoning service is unavailable right now. "
                "Try a specific office request or ask again in a moment.")
