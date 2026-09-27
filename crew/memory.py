"""Durable, scoped Slack context and links to verified crew work.

Slack history is not fetched. Conversation text here is bounded and redacted;
provider truth remains in the run, job, order, payment, and provider records.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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


def cache_reply(*, delivery_id: str, user_id: str, channel_id: str, reply: str) -> None:
    """Save a bounded reply before posting; no provider action is implied."""
    key = _delivery_hash(delivery_id)
    with SessionLocal.begin() as session:
        delivery = session.get(SlackDelivery, key, with_for_update=True)
        if delivery is None or delivery.source_user != user_id or delivery.channel_id != channel_id:
            raise ValueError("Delivery scope mismatch")
        delivery.reply_text = redact_text(reply, limit=3_000)
        delivery.updated_at = datetime.now(timezone.utc)


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
