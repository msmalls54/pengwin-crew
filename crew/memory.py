"""Durable, scoped Slack context and links to verified crew work.

Slack history is not fetched. Conversation text here is bounded and redacted;
provider truth remains in the run, job, order, payment, and provider records.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError

from .audit import record
from .config import settings
from .db import (AgentJob, ControlFlag, ConversationTurn, CrewProject, CrewRun,
                 ProjectRunLink, RunResourceLink, SessionLocal, SlackDelivery)


ROLES = {"Concierge", "Buyer", "Events", "Treasurer"}
TEXT_LIMIT = 1_000
FACTS_LIMIT = 4_000
RECENT_WINDOW = timedelta(minutes=15)
PENDING_LEASE = timedelta(minutes=3)
ORPHANED_PENDING_AFTER = timedelta(minutes=15)
PENDING_RECOVERY_LIMIT = 10
RESEND_NOTICE = ("I can't confirm whether an earlier request in this thread finished. "
                 "I won't replay it automatically. Please check its status, then send a new "
                 "message here if you still need help.")
_SLACK_TS = re.compile(r"^[0-9]{1,20}(?:\.[0-9]{1,20})?$")
_URL = re.compile(r"https?://[^\s<>]+", re.I)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_CREDENTIAL = re.compile(r"\b(?:xox[baprs]-|sk-[A-Za-z0-9_-]*|Bearer\s+)[A-Za-z0-9_.-]{10,}", re.I)
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_-]{40,}\b")
_ADDRESS = re.compile(
    r"\b\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .'-]{1,60}\s"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl)\b"
    r"(?:\s+(?:apartment|apt|suite|ste|unit|#)\s*[A-Za-z0-9-]+)?"
    r"(?:[ ,]+[A-Za-z]+(?:\s+[A-Za-z]+)?(?:,\s*[A-Za-z]+)?)?"
    r"(?:[ ,]+\d{5}(?:-\d{4})?)?",
    re.I,
)
_PRIVATE_KEY = re.compile(r"-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?-----END [^-]*PRIVATE KEY-----", re.I)
_SENSITIVE_KEY = re.compile(
    r"(?:token|secret|password|authorization|api[_-]?key|meeting[_-]?url|"
    r"email|phone|address|(?:attendee|guest)[_-]?(?:name|email|address|phone))", re.I,
)
_ACTIVE_THREAD_ROOT: ContextVar[str | None] = ContextVar("pengwin_slack_thread_root", default=None)


@dataclass(frozen=True)
class InboundClaim:
    accepted: bool
    duplicate: bool
    state: str
    run_id: str | None = None
    cached_reply: str | None = None


@dataclass(frozen=True)
class EnqueuedRun:
    run_id: str | None
    duplicate: bool
    cached_reply: str | None = None


@dataclass(frozen=True)
class ResendNotice:
    delivery_hash: str
    role: str
    user_id: str
    channel_id: str
    thread_root_ts: str


def _utc(value: datetime) -> datetime:
    """SQLite returns naive timestamps even for timezone-aware columns."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _delivery_hash(delivery_id: str) -> str:
    if not delivery_id or len(delivery_id) > 512:
        raise ValueError("A bounded Slack delivery ID is required")
    return hashlib.sha256(delivery_id.encode("utf-8")).hexdigest()


def _legacy_claim_key(delivery_hash: str) -> str:
    return "slack_delivery_" + delivery_hash[:24]


def _scope_allowed(user_id: str, channel_id: str) -> bool:
    configured = os.getenv("SLACK_DEMO_CHANNEL_ID", "")
    allowed = {
        user.strip() for key in ("SLACK_ALLOWED_USER_IDS", "SLACK_ADMIN_USER_IDS")
        for user in os.getenv(key, "").split(",") if user.strip()
    }
    return bool(configured and user_id and channel_id == configured and user_id in allowed)


def _checked_scope(role: str, user_id: str, channel_id: str) -> None:
    if role not in ROLES:
        raise ValueError("Unknown crew role")
    if not _scope_allowed(user_id, channel_id):
        raise ValueError("Slack user or channel is not allowed")


def _safe_ts(value: str | None) -> str | None:
    return value if value and _SLACK_TS.fullmatch(value) else None


def set_active_thread_root(value: str | None) -> Token:
    """Carry one inbound Slack thread root through the synchronous router."""
    return _ACTIVE_THREAD_ROOT.set(_safe_ts(value))


def reset_active_thread_root(token: Token) -> None:
    _ACTIVE_THREAD_ROOT.reset(token)


def active_thread_root() -> str | None:
    return _ACTIVE_THREAD_ROOT.get()


def redact_text(value: str, *, limit: int = TEXT_LIMIT) -> str:
    """Keep useful task language without persisting links, emails, or secrets."""
    if len(value) > 10_000:
        return "[text omitted: exceeds memory limit]"
    value = _PRIVATE_KEY.sub("[private key redacted]", value)
    value = _ADDRESS.sub("[address redacted]", value)
    value = _CREDENTIAL.sub("[credential redacted]", value)
    value = _URL.sub("[link redacted]", value)
    value = _EMAIL.sub("[email redacted]", value)
    value = _LONG_TOKEN.sub("[long token redacted]", value)
    value = "".join(character for character in value if character.isprintable() or character in "\n\t")
    return value[:limit]


def _safe_json(value: dict | None) -> str:
    def cleanse(item, depth: int = 0):
        if depth > 5:
            return "[nested value omitted]"
        if isinstance(item, dict):
            return {
                str(key)[:80]: ("[redacted]" if _SENSITIVE_KEY.search(str(key)) else cleanse(val, depth + 1))
                for key, val in list(item.items())[:30]
            }
        if isinstance(item, list):
            return [cleanse(val, depth + 1) for val in item[:30]]
        if isinstance(item, str):
            return redact_text(item, limit=500)
        if isinstance(item, (int, float, bool)) or item is None:
            return item
        return redact_text(str(item), limit=100)

    result = json.dumps(cleanse(value or {}), sort_keys=True, separators=(",", ":"))
    return result if len(result) <= FACTS_LIMIT else '{"redacted":"memory payload exceeded limit"}'


def record_inbound(*, role: str, user_id: str, channel_id: str,
                   delivery_id: str, text: str, message_ts: str | None = None,
                   thread_root_ts: str | None = None) -> InboundClaim:
    """Claim an app mention once; an abandoned PENDING claim can be recovered.

    A queued run is never recovered from here. Run creation changes the claim
    and enqueues dispatch in one database transaction.
    """
    _checked_scope(role, user_id, channel_id)
    key = _delivery_hash(delivery_id)
    now = datetime.now(timezone.utc)
    message_ts = _safe_ts(message_ts)
    thread_root_ts = _safe_ts(thread_root_ts) or message_ts
    try:
        with SessionLocal.begin() as session:
            delivery = session.get(SlackDelivery, key, with_for_update=True)
            if delivery is not None:
                if delivery.source_user != user_id or delivery.channel_id != channel_id:
                    return InboundClaim(False, True, "SCOPE_MISMATCH")
                if (delivery.state == "PENDING" and
                        now - _utc(delivery.updated_at) >= PENDING_LEASE):
                    delivery.updated_at = now
                    return InboundClaim(True, True, "RECOVERED", delivery.run_id,
                                        delivery.reply_text)
                return InboundClaim(False, True, delivery.state, delivery.run_id, delivery.reply_text)
            if session.get(ControlFlag, _legacy_claim_key(key)) is not None:
                return InboundClaim(False, True, "LEGACY_CLAIM")
            session.add(SlackDelivery(delivery_hash=key, role=role, channel_id=channel_id,
                                      source_user=user_id, state="PENDING", created_at=now,
                                      updated_at=now))
            session.add(ConversationTurn(role=role, direction="in", channel_id=channel_id,
                                         source_user=user_id, thread_root_ts=thread_root_ts,
                                         message_ts=message_ts, delivery_hash=key,
                                         content=redact_text(text), created_at=now))
        return InboundClaim(True, False, "PENDING")
    except IntegrityError:
        with SessionLocal() as session:
            delivery = session.get(SlackDelivery, key)
            if delivery is None or delivery.source_user != user_id or delivery.channel_id != channel_id:
                return InboundClaim(False, True, "SCOPE_MISMATCH")
            return InboundClaim(False, True, delivery.state, delivery.run_id, delivery.reply_text)


def cache_reply(*, delivery_id: str, user_id: str, channel_id: str, reply: str) -> bool:
    """Save a reply only while this original delivery may still respond."""
    key = _delivery_hash(delivery_id)
    with SessionLocal.begin() as session:
        delivery = session.get(SlackDelivery, key, with_for_update=True)
        if delivery is None or delivery.source_user != user_id or delivery.channel_id != channel_id:
            raise ValueError("Delivery scope mismatch")
        if delivery.state not in {"PENDING", "QUEUED"}:
            return False
        delivery.reply_text = redact_text(reply, limit=3_000)
        delivery.updated_at = datetime.now(timezone.utc)
        return True


def claim_orphaned_inbound(*, role: str, now: datetime | None = None,
                           stale_after: timedelta = ORPHANED_PENDING_AFTER,
                           limit: int = PENDING_RECOVERY_LIMIT) -> list[ResendNotice]:
    """Stop abandoned, unqueued deliveries and reserve one possible Slack notice.

    The state transition commits before any Slack call. Nothing here replays
    the saved (redacted) request or creates a run. A crash after this claim can
    lose the notice, but cannot cause an automatic second notice or task.
    """
    if role not in ROLES or stale_after <= timedelta(0) or not 1 <= limit <= PENDING_RECOVERY_LIMIT:
        raise ValueError("Invalid pending-delivery recovery request")
    now = now or datetime.now(timezone.utc)
    cutoff = now - stale_after
    notices: list[ResendNotice] = []
    with SessionLocal.begin() as session:
        candidates = session.execute(select(SlackDelivery).where(
            SlackDelivery.role == role,
            SlackDelivery.state == "PENDING",
            SlackDelivery.run_id.is_(None),
            SlackDelivery.updated_at <= cutoff,
        ).order_by(SlackDelivery.updated_at, SlackDelivery.delivery_hash)
         .limit(limit).with_for_update(skip_locked=True)).scalars().all()
        for delivery in candidates:
            changed = session.execute(update(SlackDelivery).where(
                SlackDelivery.delivery_hash == delivery.delivery_hash,
                SlackDelivery.state == "PENDING",
                SlackDelivery.run_id.is_(None),
                SlackDelivery.updated_at <= cutoff,
            ).values(state="NEEDS_RESEND", updated_at=now)
             .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                continue
            inbound = session.execute(select(ConversationTurn).where(
                ConversationTurn.delivery_hash == delivery.delivery_hash,
                ConversationTurn.direction == "in",
                ConversationTurn.role == role,
                ConversationTurn.channel_id == delivery.channel_id,
                ConversationTurn.source_user == delivery.source_user,
            ).limit(1)).scalar_one_or_none()
            thread_root = _safe_ts(inbound.thread_root_ts or inbound.message_ts) if inbound else None
            notify = bool(thread_root and _scope_allowed(delivery.source_user, delivery.channel_id))
            record(session, agent=role, action="slack_intake_needs_resend",
                   detail={"delivery_hash": delivery.delivery_hash[:12], "notice_reserved": notify},
                   severity="warning")
            if notify:
                notices.append(ResendNotice(delivery.delivery_hash, role,
                                            delivery.source_user, delivery.channel_id,
                                            thread_root))
    return notices


def record_resend_notice(*, notice: ResendNotice, message_ts: str) -> bool:
    """Store a confirmed generic Slack notice without clearing NEEDS_RESEND."""
    posted_ts = _safe_ts(message_ts)
    if not posted_ts:
        return False
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        delivery = session.get(SlackDelivery, notice.delivery_hash, with_for_update=True)
        if (delivery is None or delivery.state != "NEEDS_RESEND" or delivery.run_id is not None
                or delivery.role != notice.role or delivery.source_user != notice.user_id
                or delivery.channel_id != notice.channel_id):
            return False
        existing = session.execute(select(ConversationTurn.id).where(
            ConversationTurn.role == notice.role, ConversationTurn.direction == "out",
            ConversationTurn.delivery_hash == notice.delivery_hash,
        )).scalar_one_or_none()
        if existing is None:
            session.add(ConversationTurn(role=notice.role, direction="out",
                                         channel_id=notice.channel_id, source_user=notice.user_id,
                                         thread_root_ts=notice.thread_root_ts,
                                         message_ts=posted_ts, delivery_hash=notice.delivery_hash,
                                         content=RESEND_NOTICE, created_at=now))
        delivery.reply_text = RESEND_NOTICE
        delivery.updated_at = now
        record(session, agent=notice.role, action="slack_resend_notice_posted",
               detail={"delivery_hash": notice.delivery_hash[:12]})
    return True


def record_resend_notice_failure(*, notice: ResendNotice, reason: str) -> None:
    """Retain the one-attempt boundary when Slack gave no confirmed receipt."""
    with SessionLocal.begin() as session:
        record(session, agent=notice.role, action="slack_resend_notice_unconfirmed",
               detail={"delivery_hash": notice.delivery_hash[:12],
                       "reason": reason[:40]}, severity="warning")


def record_outbound(*, role: str, user_id: str, channel_id: str, delivery_id: str,
                    reply: str, message_ts: str | None = None,
                    thread_root_ts: str | None = None) -> None:
    """Record only a Slack-confirmed reply; failed posts are not conversation."""
    _checked_scope(role, user_id, channel_id)
    key = _delivery_hash(delivery_id)
    now = datetime.now(timezone.utc)
    message_ts = _safe_ts(message_ts)
    thread_root_ts = _safe_ts(thread_root_ts) or message_ts
    try:
        with SessionLocal.begin() as session:
            delivery = session.get(SlackDelivery, key, with_for_update=True)
            if delivery is None or delivery.source_user != user_id or delivery.channel_id != channel_id:
                raise ValueError("Delivery scope mismatch")
            existing = session.execute(select(ConversationTurn.id).where(
                ConversationTurn.role == role,
                ConversationTurn.direction == "out",
                ConversationTurn.delivery_hash == key,
            )).scalar_one_or_none()
            if existing is None:
                session.add(ConversationTurn(role=role, direction="out", channel_id=channel_id,
                                             source_user=user_id, thread_root_ts=thread_root_ts,
                                             message_ts=message_ts, delivery_hash=key,
                                             run_id=delivery.run_id, content=redact_text(reply),
                                             created_at=now))
            delivery.reply_text = redact_text(reply, limit=3_000)
            delivery.state = "RESPONDED"
            delivery.updated_at = now
    except IntegrityError:
        # A concurrent retry already stored this confirmed Slack message.
        return


def recent_turns(*, role: str, user_id: str, channel_id: str,
                 thread_root_ts: str | None = None, limit: int = 6,
                 exclude_delivery_id: str | None = None) -> list[dict]:
    """Return only this member's bounded channel/role context.

    Thread context is exact. For a new top-level message, only very recent
    turns from the same member and role are eligible; ambiguity is resolved by
    the caller rather than silently joining unrelated topics.
    """
    if role not in ROLES or not _scope_allowed(user_id, channel_id):
        return []
    limit = min(max(limit, 1), 8)
    thread_root_ts = _safe_ts(thread_root_ts)
    key = _delivery_hash(exclude_delivery_id) if exclude_delivery_id else None
    stmt = select(ConversationTurn).where(
        ConversationTurn.role == role,
        ConversationTurn.source_user == user_id,
        ConversationTurn.channel_id == channel_id,
    )
    if thread_root_ts:
        stmt = stmt.where(ConversationTurn.thread_root_ts == thread_root_ts)
    else:
        stmt = stmt.where(ConversationTurn.created_at >= datetime.now(timezone.utc) - RECENT_WINDOW)
    if key:
        stmt = stmt.where(or_(ConversationTurn.delivery_hash.is_(None),
                              ConversationTurn.delivery_hash != key))
    stmt = stmt.order_by(ConversationTurn.created_at.desc(), ConversationTurn.id.desc()).limit(limit)
    with SessionLocal() as session:
        rows = session.execute(stmt).scalars().all()
    return [{"role": row.role, "direction": row.direction, "content": row.content,
             "created_at": row.created_at, "run_id": row.run_id} for row in reversed(rows)]


def format_recent_turns(turns: list[dict], *, user_only: bool = False) -> str:
    lines = []
    for turn in turns[-8:]:
        if turn["direction"] == "in":
            lines.append("USER: " + redact_text(str(turn["content"]), limit=TEXT_LIMIT))
        elif not user_only:
            lines.append("PENGWIN " + str(turn["role"]) + ": " +
                         redact_text(str(turn["content"]), limit=TEXT_LIMIT))
    return "\n".join(lines)[:4_000]


def _fact_label(value: object, *, limit: int = 120) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(redact_text(value, limit=limit).split())[:limit] or None


def _fact_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return round(number, 2) if math.isfinite(number) and 0 <= number <= 1_000_000 else None


def _fact_count(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 10_000 else None


def _fact_date(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 50:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def _fact_date_options(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    options = []
    for item in value[:3]:
        if not isinstance(item, str):
            continue
        try:
            options.append(date.fromisoformat(item).isoformat())
        except ValueError:
            continue
    return options


def _json_object(raw: str) -> dict:
    if not isinstance(raw, str) or len(raw) > 50_000:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def latest_project_facts(*, user_id: str, channel_id: str,
                         thread_root_ts: str | None = None) -> dict | None:
    """Read the owner's latest typed project facts from its newest revision.

    Only DONE role outputs from the newest linked run count. An amendment with
    work still pending must not inherit an older quantity, estimate, or review.
    This function never invokes a provider or returns raw request/job text.
    """
    if not _scope_allowed(user_id, channel_id):
        return None
    root = _safe_ts(thread_root_ts)
    scope = (CrewProject.owner_user_id == user_id,
             CrewProject.channel_id == channel_id)
    with SessionLocal() as session:
        project = None
        if root:
            project = session.execute(select(CrewProject).where(
                *scope, CrewProject.thread_root_ts == root,
            ).limit(1)).scalar_one_or_none()
        if project is None:
            project = session.execute(select(CrewProject).where(*scope).order_by(
                CrewProject.updated_at.desc(), CrewProject.id.desc(),
            ).limit(1)).scalar_one_or_none()
        if project is None:
            return None
        linked = session.execute(select(CrewRun).join(
            ProjectRunLink, ProjectRunLink.run_id == CrewRun.id,
        ).where(
            ProjectRunLink.project_id == project.id,
            CrewRun.source_user == user_id, CrewRun.channel_id == channel_id,
        ).order_by(ProjectRunLink.id.desc()).limit(1)).scalar_one_or_none()
        by_kind: dict[str, dict] = {}
        if linked is not None:
            jobs = session.execute(select(AgentJob).where(
                AgentJob.run_id == linked.id,
                AgentJob.status == "DONE",
                AgentJob.kind.in_(("event_research", "product_source", "budget_review")),
            ).order_by(AgentJob.updated_at.desc(), AgentJob.id.desc())).scalars().all()
            for job in jobs:
                by_kind.setdefault(job.kind, _json_object(job.output_json))
        plan = _json_object(project.plan_json)
        event_plan = plan.get("event") if isinstance(plan.get("event"), dict) else None
        swag_plan = plan.get("swag") if isinstance(plan.get("swag"), dict) else None
        invite_plan = plan.get("invitations") if isinstance(plan.get("invitations"), dict) else None
        event_work = by_kind.get("event_research", {})
        product_work = by_kind.get("product_source", {})
        budget_work = by_kind.get("budget_review", {})
        venue_route = event_work.get("official_reservation_route")
        known_venue_route = (
            isinstance(venue_route, dict)
            and venue_route.get("operator") == "Transbay Joint Powers Authority"
            and venue_route.get("url") == "https://www.tjpa.org/permits-reservations"
            and venue_route.get("availability") == "UNCHECKED"
        )
        product_checked = product_work.get("publisher_status") == "ok"
        source = product_work.get("source_url")
        return {
            "project_id": project.id,
            "name": _fact_label(project.name, limit=120) or "Office project",
            "project_status": project.status if project.status in {
                "PLANNING", "ACTIVE", "WAITING_APPROVAL", "COMPLETE", "HELD", "FAILED", "REJECTED"
            } else "UNKNOWN",
            "run_status": linked.status if linked and linked.status in {
                "QUEUED", "RUNNING", "COMPLETE", "HELD", "FAILED"
            } else "UNKNOWN",
            "event": ({
                "title": _fact_label(event_plan.get("title")) or "Event",
                "date": _fact_label(event_plan.get("date_phrase"), limit=80),
                "time": _fact_label(event_plan.get("time_phrase"), limit=80),
                "venue": _fact_label(event_plan.get("venue_name")),
                "capacity": _fact_count(event_plan.get("capacity")),
                "date_options": _fact_date_options(event_work.get("date_options")),
                "venue_status": "UNCONFIRMED" if event_work.get("venue_status") == "UNCONFIRMED" else "NOT_CONFIRMED",
                "rsvp_status": "NOT_CREATED" if event_work.get("eventbrite_status") == "NOT_CREATED" else "UNVERIFIED",
                "official_inquiry_url": ("https://www.tjpa.org/permits-reservations"
                                         if known_venue_route else None),
            } if event_plan else None),
            "invitations": ({
                "status": "DRAFT_ONLY" if event_work.get("invitation_status") == "DRAFT_ONLY" else "DRAFT_PENDING",
                "draft": _fact_label(event_work.get("invitation_draft"), limit=400),
            } if invite_plan else None),
            "swag": ({
                "quantity": _fact_count(swag_plan.get("quantity")),
                "publisher_status": "ok" if product_checked else "unchecked",
                "source_url": ("https://www.printful.com/custom-water-bottles"
                               if product_checked and source == "https://www.printful.com/custom-water-bottles" else None),
                "checked_at": _fact_date(product_work.get("checked_at")),
                "currency": "USD" if product_work.get("currency") == "USD" else None,
                "unit_min": _fact_number(product_work.get("unit_min")) if product_checked else None,
                "unit_max": _fact_number(product_work.get("unit_max")) if product_checked else None,
                "subtotal_min": _fact_number(product_work.get("subtotal_min")) if product_checked else None,
                "subtotal_max": _fact_number(product_work.get("subtotal_max")) if product_checked else None,
                "checkout_status": "NOT_READY",
            } if swag_plan else None),
            "treasury": ({
                "review_status": "ESTIMATE_ONLY" if budget_work.get("review_status") == "ESTIMATE_ONLY" else "PENDING",
                "product_subtotal_min": _fact_number(budget_work.get("product_subtotal_min")),
                "product_subtotal_max": _fact_number(budget_work.get("product_subtotal_max")),
                "budget_room_cents": _fact_number(budget_work.get("internal_demo_budget_room_cents")),
                "over_budget": budget_work.get("over_internal_budget") is True,
                "payment_status": "NONE_IN_PROJECT_REVIEW" if budget_work.get("payment_status") == "NONE" else "UNVERIFIED",
                "reserved_cents": 0 if budget_work.get("reserved_cents") == 0 else None,
            } if linked else None),
        }


def format_project_facts(facts: dict | None, *, for_model: bool = False) -> str:
    """Produce a short role-neutral recap, with research and payment boundaries."""
    if facts is None:
        return "I don't see a saved project for you in this channel." if not for_model else ""
    lines = [f"Saved project: {facts['name']}."]
    if facts.get("run_status") in {"QUEUED", "RUNNING"}:
        lines.append("The latest revision is in progress; completed details below may change.")
    event = facts.get("event")
    if event:
        details = ", ".join(str(value) for value in (
            event.get("date"), event.get("time"), event.get("venue")
        ) if value)
        lines.append(f"Events: {event['title']}" + (f" ({details})" if details else "") + ".")
        if event.get("official_inquiry_url"):
            lines.append("Official permit inquiry: " + event["official_inquiry_url"] + ".")
    invites = facts.get("invitations")
    if invites:
        lines.append("Invitations: draft only; copy is ready for review."
                     if invites.get("status") == "DRAFT_ONLY" else
                     "Invitations: drafting is pending.")
    swag = facts.get("swag")
    if swag:
        quantity = swag.get("quantity")
        item = f"{quantity} water bottles" if quantity else "water bottles (quantity unconfirmed)"
        if swag.get("publisher_status") == "ok" and swag.get("currency") == "USD" and (
            swag.get("unit_min") is not None and swag.get("unit_max") is not None
        ):
            source = "Printful's official catalog"
            checked = f", checked {swag['checked_at']}" if swag.get("checked_at") else ""
            lines.append(f"Buyer: {item}; {source} product range ${swag['unit_min']:.2f}–${swag['unit_max']:.2f} per bottle{checked}.")
            if swag.get("subtotal_min") is not None and swag.get("subtotal_max") is not None:
                lines.append(f"Product-only subtotal estimate: ${swag['subtotal_min']:.2f}–${swag['subtotal_max']:.2f}.")
            if swag.get("source_url"):
                lines.append(f"Catalog source: {swag['source_url']}.")
        else:
            lines.append(f"Buyer: {item}; no current official product range is saved for this revision.")
        lines.append("Checkout is not ready: artwork, variant, destination, shipping, tax, and final total need review.")
    treasury = facts.get("treasury")
    if treasury:
        if treasury.get("review_status") == "ESTIMATE_ONLY":
            lines.append("Treasurer: estimate review only; exact checkout approval is next.")
        else:
            lines.append("Treasurer: review is pending for this revision.")
    next_steps = []
    if event:
        if not event.get("date"):
            next_steps.append("event date")
        if not event.get("time"):
            next_steps.append("start time")
        if invites and not event.get("capacity"):
            next_steps.append("RSVP capacity")
    if swag:
        next_steps.append("bottle artwork, variant, and delivery destination")
    if next_steps:
        lines.append("Next: confirm " + "; ".join(next_steps) + ".")
    status = []
    if event:
        status.append("Venue availability and booking are unconfirmed")
        status.append("RSVP page not created" if event.get("rsvp_status") == "NOT_CREATED" else "RSVP status unverified")
    if invites:
        status.append("invitations not sent by this workflow")
    if treasury and treasury.get("payment_status") == "NONE_IN_PROJECT_REVIEW" and treasury.get("reserved_cents") == 0:
        status.append("no funds reserved or payment action in this project workflow")
    if status:
        lines.append("Status: " + "; ".join(status) + ".")
    prefix = "OWNER-SCOPED SAVED PROJECT FACTS (not spending or booking approval):\n" if for_model else ""
    return (prefix + "\n".join(lines))[:1_500]


def format_concierge_project_facts(facts: dict | None) -> str:
    """Lead an owner-scoped Slack recap with saved work and one decision."""
    if facts is None:
        return "I don't see a saved project for you in this channel."
    lines = [f"Saved plan: {facts['name']}."]
    if facts.get("run_status") in {"QUEUED", "RUNNING"}:
        lines.append("The latest revision is in progress; its estimates may change.")
    event = facts.get("event") or {}
    invites = facts.get("invitations") or {}
    swag = facts.get("swag") or {}
    treasury = facts.get("treasury") or {}
    options = event.get("date_options") or []
    if event:
        venue = event.get("venue") or event.get("title")
        detail = [venue]
        if options:
            labels = [f"{date.fromisoformat(value):%b} {date.fromisoformat(value).day}"
                      for value in options]
            detail.append("date options " + ", ".join(labels))
        elif event.get("date"):
            detail.append("timing " + event["date"])
        if event.get("time"):
            detail.append(event["time"])
        if event.get("capacity"):
            detail.append(f"{event['capacity']} guests")
        lines.append("Events: " + "; ".join(detail) + ".")
        if event.get("official_inquiry_url"):
            lines.append("Venue inquiry: " + event["official_inquiry_url"] + ".")
    if invites.get("draft"):
        lines.append("Invitation draft: " + invites["draft"])
    elif invites.get("status") == "DRAFT_ONLY":
        lines.append("Events prepared invitation copy for review.")
    if swag:
        count = f"{swag['quantity']} water bottles" if swag.get("quantity") else "water bottles"
        if (swag.get("publisher_status") == "ok" and swag.get("currency") == "USD"
                and swag.get("subtotal_min") is not None and swag.get("subtotal_max") is not None):
            lines.append(f"Buyer: sourced {count} from Printful; product-only estimate "
                         f"${swag['subtotal_min']:.2f}–${swag['subtotal_max']:.2f} before shipping and tax.")
            if swag.get("source_url"):
                lines.append("Product source: " + swag["source_url"] + ".")
        else:
            lines.append(f"Buyer: researching a current product price for {count}.")
    if treasury.get("review_status") == "ESTIMATE_ONLY":
        room = treasury.get("budget_room_cents")
        high = treasury.get("product_subtotal_max")
        if room is not None and high is not None:
            judgment = "exceeds" if treasury.get("over_budget") else "fits within"
            lines.append(f"Treasurer: the product-only estimate {judgment} the "
                         f"${room / 100:,.2f} available in the recorded swag allocation.")
        else:
            lines.append("Treasurer: recorded the product estimate for budget review.")
    next_steps = []
    if options:
        next_steps.append("choose one date")
    elif event and not event.get("date"):
        next_steps.append("confirm a date")
    if event and not event.get("time"):
        next_steps.append("confirm the start time and duration")
    if invites and event and not event.get("capacity"):
        next_steps.append("set RSVP capacity")
    if swag:
        next_steps.append("send bottle artwork, variant, and delivery destination for an exact checkout total")
    if next_steps:
        lines.append("Next: " + "; ".join(next_steps) + ".")
    boundaries = []
    if event:
        boundaries.extend(("venue booking", "RSVP publication"))
    if swag:
        boundaries.extend(("bottle order", "payment"))
    if invites:
        boundaries.append("invitation send")
    if boundaries:
        lines.append("Status: no " + ", ".join(boundaries) + " recorded for this plan.")
    return "\n".join(lines)[:1_500]


def is_project_role_detail_request(role: str, text: str) -> bool:
    """Route read-only questions about completed role work before generic chat."""
    value = " ".join(text.casefold().split())
    if re.search(r"\b(?:approve|buy|order|pay|send|transfer|book|reserve|publish)\b", value):
        return False
    if not re.search(r"\b(?:show|what|which|review|does|how|remind)\b", value):
        return False
    if role == "Events":
        return bool(re.search(r"\b(?:date options?|venue inquiry|reservation route|invitation draft|invite copy)\b", value))
    if role == "Buyer":
        return bool(re.search(r"\b(?:bottles?|product estimate|checkout.ready quote)\b", value)
                    and re.search(r"\b(?:source|sourced|estimate|quote|checkout)\b", value))
    if role == "Treasurer":
        if re.search(r"\b(?:spent|spend|paid|payment|charges?|reserved|funds)\b", value):
            return False
        return bool(re.search(r"\b(?:bottles?|swag|meetup|project)\b", value)
                    and re.search(r"\b(?:estimate|budget|allocation|fit)\b", value))
    return False


def format_project_role_details(role: str, facts: dict | None) -> str:
    """Answer from the latest completed, owner-scoped project jobs only."""
    if facts is None:
        return "I don't see a saved project for you in this channel."
    if role == "Events":
        event = facts.get("event")
        if not event:
            return "I don't see completed event planning for this project yet."
        title = event["title"] if event["title"] != "Event" else facts["name"]
        lines = [f"For {title}, here is the saved event work:"]
        options = event.get("date_options") or []
        if options:
            labels = [f"{date.fromisoformat(value):%a %b} {date.fromisoformat(value).day}, {date.fromisoformat(value).year}"
                      for value in options]
            lines.append("Date options: " + ", ".join(labels) + ".")
        elif event.get("date"):
            lines.append("Requested timing: " + event["date"] + ".")
        if event.get("official_inquiry_url"):
            lines.append("Official venue inquiry: " + event["official_inquiry_url"] + ".")
        else:
            lines.append("No official venue inquiry route is saved for this revision.")
        invitations = facts.get("invitations") or {}
        if invitations.get("draft"):
            lines.append("Invitation draft: " + invitations["draft"])
        next_steps = []
        if options:
            next_steps.append("choose one date")
        elif not event.get("date"):
            next_steps.append("confirm a date")
        if not event.get("time"):
            next_steps.append("confirm the start time and duration")
        if invitations and not event.get("capacity"):
            next_steps.append("set RSVP capacity")
        if next_steps:
            lines.append("Next: " + "; ".join(next_steps) + ".")
        lines.append("Status: venue availability is unconfirmed; no reservation or Eventbrite publication for this plan.")
        return "\n".join(lines)[:1_500]
    if role == "Buyer":
        swag = facts.get("swag")
        if not swag:
            return "I don't see bottle sourcing for this project yet."
        quantity = swag.get("quantity")
        item = f"{quantity} custom water bottles" if quantity else "custom water bottles"
        lines = [f"I checked a product source for {item}."]
        if (swag.get("publisher_status") == "ok" and swag.get("currency") == "USD"
                and swag.get("unit_min") is not None and swag.get("unit_max") is not None):
            lines.append(f"Printful catalog: ${swag['unit_min']:.2f}–${swag['unit_max']:.2f} per bottle.")
            if swag.get("subtotal_min") is not None and swag.get("subtotal_max") is not None:
                lines.append(f"Product estimate: ${swag['subtotal_min']:.2f}–${swag['subtotal_max']:.2f}, before shipping and tax.")
            if swag.get("source_url"):
                lines.append("Source: " + swag["source_url"] + ".")
        else:
            lines.append("No current official product price is saved for this revision.")
        lines.append("Next: Send print-ready artwork, choose the bottle variant, and confirm the delivery destination. I'll bring back an exact checkout total for approval.")
        lines.append("Status: catalog estimate only; no checkout submitted.")
        return "\n".join(lines)[:1_500]
    if role == "Treasurer":
        treasury = facts.get("treasury")
        if not treasury or treasury.get("review_status") != "ESTIMATE_ONLY":
            return "The budget review for this project is still pending."
        swag = facts.get("swag") or {}
        item = f"{swag['quantity']} water bottles" if swag.get("quantity") else "the water bottles"
        lines = [f"I reviewed {item} for {facts['name']}."]
        low = treasury.get("product_subtotal_min")
        high = treasury.get("product_subtotal_max")
        if low is not None and high is not None:
            lines.append(f"Product estimate: ${low:.2f}–${high:.2f}, before shipping and tax.")
            room = treasury.get("budget_room_cents")
            if room is not None:
                judgment = "exceeds" if treasury.get("over_budget") else "fits within"
                lines.append(f"This product-only estimate {judgment} the ${room / 100:,.2f} available in the recorded swag allocation.")
        else:
            lines.append("No current product estimate is saved for this revision.")
        lines.append("Next: Bring the exact checkout total, including shipping and tax, for approval against this purpose.")
        return "\n".join(lines)[:1_500]
    raise ValueError("Unknown project role")


def combine_context(recent_turns: str, project_facts: str) -> str:
    """Keep the newest complete conversation lines plus bounded saved facts."""
    project_facts = project_facts[:1_500]
    budget = 4_000 - len(project_facts) - (1 if project_facts else 0)
    kept = []
    remaining = budget
    for line in reversed(recent_turns.splitlines()):
        if not line.startswith(("USER:", "PENGWIN ")):
            continue
        if len(line) + 1 > remaining:
            break
        kept.append(line)
        remaining -= len(line) + 1
    parts = ["\n".join(reversed(kept))] if kept else []
    if project_facts:
        parts.append(project_facts)
    return "\n".join(parts)


def is_project_recall_request(text: str) -> bool:
    """Match explicit recaps without intercepting live provider-status requests."""
    value = " ".join(text.casefold().split())
    if (re.search(r"\b(?:summari[sz]e|recap|list|what|which)\b", value)
            and re.search(r"\b(?:unconfirmed|unknown|unchecked|pending)\b", value)
            and re.search(r"\b(?:this|our|my|the)\s+(?:plan|project)\b", value)):
        return True
    if (re.search(r"\b(?:latest|last)\s+(?:saved\s+)?project\b", value)
            and re.search(r"\b(?:what|which|show|tell|give|recap|summary|remind|status|estimate)\b", value)):
        return True
    if re.search(
        r"\b(?:what have we planned|what did we plan|what'?s (?:our|my|the) plan|"
        r"remind me (?:of|about) (?:our|my|the) (?:plan|project)|"
        r"(?:what happened|where are we|status|update|recap|summary).{0,50}(?:our|my|the) project|"
        r"(?:our|my|the) project.{0,50}(?:status|update|recap|summary))\b",
        value,
    ):
        return True
    if re.search(r"\b(?:what happened|where are we|status|update|recap|summary)\b", value):
        return bool(re.search(r"\b(?:bottles?|invitations?|venue)\b", value))
    return bool(
        re.search(r"\b(?:did we|have we|were|was)\b", value)
        and re.search(r"\b(?:bottles?|invitations?|venue)\b", value)
        and re.search(r"\b(?:pay|paid|spend|spent|cost|purchase|buy|bought|order|ordered|send|sent|book|booked|reserve|reserved)\b", value)
    )


def enqueue_delivery_run(*, flow: str, user_id: str, channel_id: str,
                         delivery_id: str, request_text: str | None = None,
                         reply_text: str | None = None,
                         context: str = "", thread_root_ts: str | None = None) -> EnqueuedRun:
    """Atomically claim a delivery, create its run, and queue Concierge.

    This mirrors the established submit_run validation but shares a transaction
    with the Slack delivery. A crash can leave a recoverable PENDING inbound
    claim, but can never leave a QUEUED claim without its run.
    """
    from .jobs import FLOW_STEPS, _new_job

    if not _scope_allowed(user_id, channel_id):
        raise ValueError("Run source is not allowed in the configured Slack channel")
    if flow not in FLOW_STEPS and flow not in {"natural-language", "luma-event", "eventbrite-event", "code-task", "project-plan", "event-status"}:
        raise ValueError("Unknown crew flow")
    if flow in {"natural-language", "luma-event", "eventbrite-event", "code-task", "project-plan", "event-status"}:
        if flow == "natural-language" and settings.payment_mode != "simulated":
            raise ValueError("Natural-language requests require simulated payment mode")
        if not request_text or len(request_text) > 1_000:
            raise ValueError("Request must contain at most 1000 characters")
    key = _delivery_hash(delivery_id)
    now = datetime.now(timezone.utc)
    try:
        with SessionLocal.begin() as session:
            delivery = session.get(SlackDelivery, key, with_for_update=True)
            if delivery is None:
                if session.get(ControlFlag, _legacy_claim_key(key)) is not None:
                    return EnqueuedRun(None, True, None)
                delivery = SlackDelivery(delivery_hash=key, role="Concierge", channel_id=channel_id,
                                         source_user=user_id, state="PENDING", created_at=now,
                                         updated_at=now)
                session.add(delivery)
                session.flush()
            elif delivery.source_user != user_id or delivery.channel_id != channel_id:
                return EnqueuedRun(None, True, None)
            if delivery.run_id is not None:
                return EnqueuedRun(delivery.run_id, True, delivery.reply_text)
            claimed = session.execute(update(SlackDelivery).where(
                SlackDelivery.delivery_hash == key, SlackDelivery.state == "PENDING",
            ).values(state="ENQUEUING", updated_at=now))
            if claimed.rowcount != 1:
                session.expire(delivery)
                return EnqueuedRun(delivery.run_id, True, delivery.reply_text)
            inbound = session.execute(select(ConversationTurn.id).where(
                ConversationTurn.direction == "in", ConversationTurn.delivery_hash == key,
            )).scalar_one_or_none()
            if inbound is None:
                session.add(ConversationTurn(role=delivery.role, direction="in",
                                             channel_id=channel_id, source_user=user_id,
                                             delivery_hash=key,
                                             content=redact_text(request_text or flow),
                                             created_at=now))
            run_id = str(uuid4())
            session.add(CrewRun(id=run_id, flow=flow, source_user=user_id,
                                channel_id=channel_id, status="QUEUED"))
            session.flush()
            input_data = {"flow": flow}
            if request_text:
                input_data["text"] = request_text
            if context and flow in {"project-plan", "event-status"}:
                input_data["context"] = redact_text(context, limit=4_000)
            if _safe_ts(thread_root_ts):
                input_data["thread_root_ts"] = thread_root_ts
            _new_job(session, run_id=run_id, role="Concierge", kind="dispatch", input_data=input_data)
            record(session, agent="Concierge", action="run_queued", request_id=run_id,
                   detail={"flow": flow, "source_user": user_id})
            delivery.run_id = run_id
            delivery.state = "QUEUED"
            delivery.reply_text = redact_text(reply_text, limit=3_000) if reply_text else None
            delivery.updated_at = now
            session.execute(update(ConversationTurn).where(
                ConversationTurn.direction == "in", ConversationTurn.delivery_hash == key,
            ).values(run_id=run_id))
        return EnqueuedRun(run_id, False, reply_text)
    except IntegrityError:
        with SessionLocal() as session:
            delivery = session.get(SlackDelivery, key)
            if delivery is None or delivery.source_user != user_id or delivery.channel_id != channel_id:
                return EnqueuedRun(None, True, None)
            return EnqueuedRun(delivery.run_id, True, delivery.reply_text)


def create_or_update_project(*, run_id: str, owner_user_id: str, channel_id: str,
                             name: str, safe_plan: dict | None = None,
                             thread_root_ts: str | None = None,
                             status: str = "ACTIVE") -> str:
    """Reuse a project for amendments in the same owner's Slack thread."""
    _checked_scope("Concierge", owner_user_id, channel_id)
    if status not in {"PLANNING", "ACTIVE", "WAITING_APPROVAL", "COMPLETE", "HELD", "FAILED", "REJECTED"}:
        raise ValueError("Unknown project status")
    now = datetime.now(timezone.utc)
    thread_root_ts = _safe_ts(thread_root_ts)
    with SessionLocal.begin() as session:
        run = session.get(CrewRun, run_id)
        if run is None or run.source_user != owner_user_id or run.channel_id != channel_id:
            raise ValueError("Project run scope mismatch")
        existing_link = session.execute(select(ProjectRunLink).where(
            ProjectRunLink.run_id == run_id,
        )).scalar_one_or_none()
        project = session.get(CrewProject, existing_link.project_id) if existing_link else None
        if project is None and thread_root_ts:
            # Serialize amendments to a known thread on Postgres. The first
            # inbound turn exists before the dispatch job is queued.
            session.execute(select(ConversationTurn.id).where(
                ConversationTurn.channel_id == channel_id,
                ConversationTurn.source_user == owner_user_id,
                ConversationTurn.thread_root_ts == thread_root_ts,
            ).order_by(ConversationTurn.id).limit(1).with_for_update()).first()
            project = session.execute(select(CrewProject).where(
                CrewProject.channel_id == channel_id,
                CrewProject.owner_user_id == owner_user_id,
                CrewProject.thread_root_ts == thread_root_ts,
            ).with_for_update()).scalar_one_or_none()
        if project is None:
            project = CrewProject(id=run_id, owner_user_id=owner_user_id, channel_id=channel_id,
                                  name=redact_text(name, limit=160),
                                  thread_root_ts=thread_root_ts,
                                  plan_json=_safe_json(safe_plan), status=status,
                                  created_at=now, updated_at=now)
            session.add(project)
        else:
            project.name = redact_text(name, limit=160)
            if thread_root_ts:
                project.thread_root_ts = thread_root_ts
            if safe_plan is not None:
                project.plan_json = _safe_json(safe_plan)
            project.status = status
            project.updated_at = now
        link = session.execute(select(ProjectRunLink.id).where(
            ProjectRunLink.project_id == project.id, ProjectRunLink.run_id == run_id,
        )).scalar_one_or_none()
        if link is None:
            session.add(ProjectRunLink(project_id=project.id, run_id=run_id, created_at=now))
        project_id = project.id
    return project_id


def link_run_resource(*, run_id: str, role: str, resource_type: str,
                      resource_id: str, safe_facts: dict | None = None,
                      visibility: str = "owner") -> None:
    """Associate a durable run with a resource; current status needs readback."""
    if role not in ROLES or visibility not in {"owner", "channel_public"}:
        raise ValueError("Invalid resource role or visibility")
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", resource_type):
        raise ValueError("Invalid resource type")
    if not resource_id or len(resource_id) > 160:
        raise ValueError("Invalid resource ID")
    now = datetime.now(timezone.utc)
    with SessionLocal.begin() as session:
        if session.get(CrewRun, run_id) is None:
            raise ValueError("Resource run is missing")
        link = session.execute(select(RunResourceLink).where(
            RunResourceLink.run_id == run_id,
            RunResourceLink.resource_kind == resource_type,
            RunResourceLink.resource_id == resource_id,
        )).scalar_one_or_none()
        if link is None:
            session.add(RunResourceLink(run_id=run_id, role=role,
                                        resource_kind=resource_type, resource_id=resource_id,
                                        visibility=visibility, facts_json=_safe_json(safe_facts),
                                        created_at=now, updated_at=now))
        else:
            if safe_facts is not None:
                link.facts_json = _safe_json(safe_facts)
            link.visibility = visibility
            link.updated_at = now
