"""Bounded, plain-English Slack conversations for the four Pengwin identities."""

from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from .db import Payment, SessionLocal
from .inference import VultrInference
from .project_planner import (SourcingProposal, generic_sourcing_proposal,
                              is_multi_part_project_request, user_request_source)
from .research import ResearchResult, lookup_project_facts
from .slack_bot import (FLOW_COMMANDS, latest_run_status_text, queue_allowed_flow,
                        run_status_text, status_text)


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
        return f"I don't see any demo checkouts or Airwallex test transfers {label}."
    parts = []
    for status, currency, total, count in rows:
        description = "demo checkout" if status == "SIMULATED" else "Airwallex test transfer"
        ending = " (no real card charged)" if status == "SIMULATED" else " (settlement not confirmed)"
        parts.append(f"{_money(int(total), currency)} across {count} {description}{'' if count == 1 else 's'}{ending}")
    return f"Here's what I recorded {label}:\n" + "\n".join(parts)


def _looks_like_purchase(text: str) -> bool:
    def prohibited(start: int) -> bool:
        # Look only inside the current clause, so "do not order X; buy Y"
        # still recognizes the later positive instruction.
        clause = re.split(r"[.!?;]|\b(?:but|instead)\b", text[:start], flags=re.I)[-1]
        return bool(re.search(
            r"\b(?:do\s+not|don['’]?t|never|not\s+to|no)\s+"
            r"(?:(?:please|ever|actually|immediately|yet|want\s+to|need\s+to)\s+)?$",
            clause, re.I,
        ))

    for match in re.finditer(r"\b(order|buy|purchase|replenish|restock|pick up|shop for)\b", text, re.I):
        if not prohibited(match.start()):
            return True
    need = re.search(r"\b(need|want|get)\b", text, re.I)
    return bool(need and not prohibited(need.start()) and
                re.search(r"\b\d+\b", text) and
                re.search(r"\b(oat milk|coffee|hoodie|welcome kit|supplies)\b", text, re.I))


def _looks_like_event_request(text: str) -> bool:
    return bool(re.search(r"\b(set up|schedule|organize|plan|create|book|arrange|invite)\b", text, re.I) and
                re.search(r"\b(event|lunch|meeting|party|gathering|invite)\b", text, re.I))


def _looks_like_event_status(text: str, context: str = "") -> bool:
    """Route live-event questions and short date follow-ups to a facts worker."""
    status_words = re.compile(
        r"\b(live|published|upcoming|current|currently|status|rsvp|link|listed|created)\b", re.I
    )
    event_words = re.compile(r"\b(events?|eventbrite|rsvp pages?)\b", re.I)
    if event_words.search(text) and status_words.search(text):
        return True
    followup = re.sub(r"^(?:(?:and\s+)?(?:what|how)\s+about\s+|and\s+)",
                      "", text.strip(), count=1, flags=re.I)
    if not re.fullmatch(
        r"(?:on |the one on |for )?"
        r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|"
        r"jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
        r"\s+\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?[?.! ]*",
        followup, re.I,
    ):
        return False
    try:
        prior_user_words = user_request_source("follow-up", context)
    except ValueError:
        return False
    return bool(event_words.search(prior_user_words) and status_words.search(prior_user_words))


def _safe_product_query(text: str) -> str | None:
    """Map broad product terms to generic queries without sending raw Slack text."""
    products = (
        (r"\bwater\s+bottles?\b|\bdrinkware\b", "custom water bottles business suppliers"),
        (r"\bchairs?\b", "office chairs business suppliers"),
        (r"\bdesks?\b", "office desks business suppliers"),
        (r"\bmonitors?\b", "office monitors business suppliers"),
        (r"\bkeyboards?\b", "office keyboards business suppliers"),
        (r"\blaptops?\b", "business laptops suppliers"),
        (r"\bprinters?\b", "office printers business suppliers"),
        (r"\b(?:espresso|coffee) machines?\b", "office coffee machines business suppliers"),
        (r"\bmugs?\b", "custom mugs business suppliers"),
        (r"\bnotebooks?\b", "office notebooks business suppliers"),
    )
    for pattern, query in products:
        if re.search(pattern, text, re.I):
            return query
    return None


def _external_research_query(text: str) -> tuple[str, str] | None:
    if re.search(r"\b(approve|publish|pay|transfer|refund|reimburse|charge)\b", text, re.I):
        return None
    if not ("?" in text or re.search(
        r"\b(find|search|look up|research|compare|current|latest|price|cost|available)\b",
        text, re.I,
    )):
        return None
    product_query = _safe_product_query(text)
    if product_query:
        return product_query, "web"
    if re.search(r"\bsalesforce\s+park\b", text, re.I):
        return "Salesforce Park San Francisco event reservation official", "web"
    if re.search(r"\b(venue|meeting space|event space|restaurant|park)\b", text, re.I):
        city = " San Francisco" if re.search(r"\b(San Francisco|SF)\b", text, re.I) else (
            " Berlin" if re.search(r"\bBerlin\b", text, re.I) else ""
        )
        return f"event venues{city}", "place"
    if re.search(r"\beventbrite\b", text, re.I):
        return "Eventbrite event registration official help", "web"
    return None


def _search_context(result: ResearchResult) -> str:
    if result.status != "ok":
        return "Live search unavailable; current external facts remain unverified."
    if not result.results:
        return "Live search returned no leads; current external facts remain unverified."
    lines = ["Unverified search leads; not checked with the publisher:"]
    for lead in result.results[:2]:
        lines.append(f"{lead.title}: {lead.snippet[:150]} {lead.url or ''}".strip())
    return "\n".join(lines)[:1500]


def _sourcing_reply(proposal: SourcingProposal) -> str:
    """Do research for unsupported goods without entering a mock checkout."""
    lead = (f"I can source options for {proposal.quantity} {proposal.product_phrase}. "
            "I have not placed an order or obtained a checkout quote.")
    query = _safe_product_query(proposal.product_phrase)
    if query:
        result = lookup_project_facts(query, kind="web", max_results=2)
        if result.status == "ok" and result.results:
            choices = [f"{item.title} ({item.url})" for item in result.results if item.url]
            if choices:
                lead += " Possible suppliers to check: " + "; ".join(choices[:2]) + "."
            else:
                lead += " The search returned no usable supplier links."
        else:
            lead += " Live supplier search is unavailable."
    else:
        lead += " I need the product type or specifications before a useful supplier search."
    return (lead + " A real purchase needs a business checkout path, delivery details, "
            "a current total, and your approval.")[:1800]


def answer(role: str, text: str, *, user_id: str, channel_id: str,
           delivery_id: str, thread_ts: str | None = None,
           context: str = "") -> str:
    """Only established workflows may mutate outside Slack; chat itself is read-only."""
    if role not in {"Concierge", "Buyer", "Events", "Treasurer"}:
        raise ValueError("Unknown crew role")
    text = text.strip()
    if not text:
        return f"I'm Pengwin {role}. Tell me what you need in plain English."
    if len(text) > 1000:
        return "Keep the request under 1,000 characters so I can handle it cleanly."
    lower = text.lower()
    decision = lower.strip(" .!")
    if role in {"Concierge", "Events"}:
        choices = {
            "approve this event": "approve", "approve the event": "approve",
            "publish this event": "approve", "publish the event": "approve",
            "looks good, publish it": "approve", "yes, publish it": "approve",
            "cancel this draft": "reject", "reject this event": "reject",
            "review this event": "review", "show this draft again": "review",
        }
        if decision in choices:
            from .jobs import decide_event_in_thread
            return decide_event_in_thread(thread_ts=thread_ts, user_id=user_id,
                                          channel_id=channel_id, action=choices[decision])
    if role == "Treasurer":
        if re.search(r"\b(spent|spend|paid|payments|charges|outflow)\b", lower):
            return spend_report(text)
        if re.search(r"\b(budget|remaining|left to spend)\b", lower):
            return status_text(budgets=True)
        if re.search(r"\b(send|transfer|pay|reimburse|refund)\b", lower):
            return ("I can check the budget, but I won't send money from a chat message. "
                    "Ask Concierge to set up a supported request for review. Real payments are not enabled here.")
    if lower.startswith("run "):
        return run_status_text(text[4:].strip(), user_id=user_id)
    if role in {"Concierge", "Events"} and _looks_like_event_status(text, context):
        return queue_allowed_flow("event-status", user_id=user_id, channel_id=channel_id,
                                  delivery_id=delivery_id, request_text=text, context=context)
    if re.search(r"\b(status|progress|update|happened|going)\b", lower) and re.search(
        r"\b(my|last|order|event|request|task)\b", lower
    ):
        return latest_run_status_text(user_id=user_id, channel_id=channel_id)
    if role == "Concierge":
        if lower in FLOW_COMMANDS:
            return queue_allowed_flow(FLOW_COMMANDS[lower], user_id=user_id, channel_id=channel_id,
                                      delivery_id=delivery_id)
        if lower in {"status", "crew status"}:
            return status_text(budgets=False)
        if lower in {"budgets", "budget"}:
            return status_text(budgets=True)
    if role in {"Concierge", "Events"} and is_multi_part_project_request(text, context):
        return queue_allowed_flow("project-plan", user_id=user_id, channel_id=channel_id,
                                  delivery_id=delivery_id, request_text=text, context=context)
    if role in {"Concierge", "Buyer"}:
        sourcing = generic_sourcing_proposal(text)
        if sourcing:
            return _sourcing_reply(sourcing)
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
    search_result = None
    research_context = ""
    lookup = _external_research_query(text)
    if lookup:
        query, kind = lookup
        search_result = lookup_project_facts(query, kind=kind, max_results=2)
        research_context = _search_context(search_result)
    try:
        reply = VultrInference().role_reply(role=role, request_text=text, facts=facts,
                                            context=context, research_context=research_context)
        if search_result and search_result.status != "ok" and "live search" not in reply.casefold():
            reply += " Live search is unavailable, so current external details remain unverified."
        return reply[:1500]
    except Exception:
        # A model outage should not pretend a task or factual answer succeeded.
        return ("I can't think through that request right now. Try again in a moment, "
                "or give me a specific office task to queue.")
