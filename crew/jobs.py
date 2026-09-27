"""Durable, role-routed office workflows.

Only queued jobs are claimable. A claimed job is never retried automatically:
after a crash it must be inspected before a person decides whether it is safe
to resume. This is especially important around payment submission.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .audit import record
from .config import settings
from .db import (AgentJob, Budget, ControlFlag, ConversationTurn, CrewRun, PendingOrder,
                 ProjectRunLink, Request, RunResourceLink, SessionLocal)
from .eventbrite import (EventbriteClient, approval_preview as eventbrite_preview,
                         checked_eventbrite_plan, published_message)
from .inference import VultrInference
from .intake import checked_plan
from .luma import LumaClient, LumaPlan, approval_preview, checked_luma_plan, plan_snapshot
from .payments import pay_pending_order
from .sandbox import sandbox
from .slack_outbound import post_role_update
from .workflow import (
    complete_berlin_lunch,
    prepare_berlin_lunch,
    prepare_purchase,
    requote_purchase,
)


FLOW_STEPS: dict[str, tuple[tuple[str, str, dict], ...]] = {
    "berlin-pantry": (
        ("Buyer", "purchase", {"office": "BER", "sku": "OAT-MILK", "qty": 6, "text": "Berlin is out of oat milk"}),
        ("Buyer", "purchase", {"office": "BER", "sku": "COFFEE", "qty": 2, "text": "Berlin needs coffee"}),
    ),
    "welcome-kit": (
        ("Buyer", "purchase", {"office": "BER", "sku": "WELCOME-KIT", "qty": 1, "text": "New hire starts in Berlin Monday"}),
        ("Events", "lunch_prepare", {"headcount": 8}),
    ),
    "hoodie-attack": (
        ("Buyer", "purchase", {"office": "BER", "sku": "HOODIE-BER", "qty": 20, "text": "Hoodies for the Berlin office"}),
        ("Buyer", "purchase", {"office": "SF", "sku": "HOODIE-SF", "qty": 40, "text": "Hoodies for the San Francisco office"}),
    ),
}
FLOW_LABELS = {
    "berlin-pantry": "Berlin pantry",
    "welcome-kit": "Berlin welcome",
    "hoodie-attack": "company hoodies",
    "natural-language": "your request",
    "project-plan": "your event project",
    "event-status": "your event lookup",
    "luma-event": "a Luma event draft",
    "eventbrite-event": "a public RSVP event draft",
    "code-task": "a code task",
}
ITEM_LABELS = {
    "OAT-MILK": ("oat milk carton", "oat milk cartons"),
    "COFFEE": ("coffee bag", "coffee bags"),
    "HOODIE-BER": ("Berlin hoodie", "Berlin hoodies"),
    "HOODIE-SF": ("San Francisco hoodie", "San Francisco hoodies"),
    "WELCOME-KIT": ("welcome kit", "welcome kits"),
}


def flow_label(flow: str) -> str:
    return FLOW_LABELS.get(flow, flow.replace("-", " "))

ROLE_KINDS = {
    "Concierge": {"dispatch"},
    "Buyer": {"purchase", "requote", "code_execute", "product_source"},
    "Events": {"lunch_prepare", "lunch_complete", "luma_publish", "eventbrite_publish", "event_research", "eventbrite_status"},
    "Treasurer": {"pay", "budget_review"},
}
UNCERTAIN_PAYMENT_STATUSES = {"PENDING_PROVIDER", "PROVIDER_OUTCOME_UNKNOWN"}


def _new_job(session: Session, *, run_id: str, role: str, kind: str, input_data: dict) -> AgentJob:
    if kind not in ROLE_KINDS.get(role, set()):
        raise ValueError("Invalid role capability")
    job = AgentJob(id=str(uuid4()), run_id=run_id, role=role, kind=kind,
                   input_json=json.dumps(input_data, sort_keys=True), status="QUEUED")
    session.add(job)
    return job


def submit_run(flow: str, *, source_user: str, channel_id: str,
               request_text: str | None = None, context: str = "") -> str:
    if flow not in FLOW_STEPS and flow not in {"natural-language", "project-plan", "event-status", "luma-event", "eventbrite-event", "code-task"}:
        raise ValueError("Unknown crew flow")
    if flow in {"natural-language", "project-plan", "event-status", "luma-event", "eventbrite-event", "code-task"}:
        if flow == "natural-language" and settings.payment_mode != "simulated":
            raise ValueError("Natural-language requests require simulated payment mode")
        if not request_text or len(request_text) > 1000:
            raise ValueError("Request must contain at most 1000 characters")
    configured_channel = os.getenv("SLACK_DEMO_CHANNEL_ID", "")
    allowed_users = {
        user.strip() for key in ("SLACK_ALLOWED_USER_IDS", "SLACK_ADMIN_USER_IDS")
        for user in os.getenv(key, "").split(",") if user.strip()
    }
    if not configured_channel or channel_id != configured_channel or source_user not in allowed_users:
        raise ValueError("Run source is not allowed in the configured Slack channel")
    run_id = str(uuid4())
    with SessionLocal.begin() as session:
        session.add(CrewRun(id=run_id, flow=flow, source_user=source_user,
                            channel_id=channel_id, status="QUEUED"))
        # AgentJob references crew_runs; without an ORM relationship SQLAlchemy
        # can flush the job first on PostgreSQL, violating its foreign key.
        session.flush()
        input_data = {"flow": flow}
        if request_text:
            input_data["text"] = request_text
        if flow in {"project-plan", "event-status"} and context:
            input_data["context"] = context[:1000]
        _new_job(session, run_id=run_id, role="Concierge", kind="dispatch", input_data=input_data)
        record(session, agent="Concierge", action="run_queued", request_id=run_id,
               detail={"flow": flow, "source_user": source_user})
    return run_id


def submit_web_code_run(goal: str) -> str:
    """A token-authenticated web user may queue only offline code work."""
    if settings.sandbox_mode != "docker" or settings.planner_mode != "vultr":
        raise ValueError("Web code tasks require Vultr inference and the Docker sandbox VM")
    if not goal.strip() or len(goal) > 1000:
        raise ValueError("Goal must contain 1–1000 characters")
    with SessionLocal.begin() as session:
        prior = session.execute(select(CrewRun.id).where(
            CrewRun.source_user == "web-demo",
        ).limit(30)).all()
        if len(prior) >= 30:
            raise ValueError("Web demo run limit reached")
        run_id = str(uuid4())
        session.add(CrewRun(id=run_id, flow="code-task", source_user="web-demo",
                            channel_id="web", status="QUEUED"))
        session.flush()
        _new_job(session, run_id=run_id, role="Concierge", kind="dispatch",
                 input_data={"flow": "code-task", "text": goal.strip()})
        record(session, agent="Concierge", action="web_code_queued", request_id=run_id,
               detail={"goal_chars": len(goal.strip())})
    return run_id


def submit_web_demo_run(flow: str) -> str:
    if flow not in FLOW_STEPS:
        raise ValueError("Unknown demo flow")
    if settings.payment_mode != "simulated":
        raise ValueError("Web demo flows require simulated payment mode")
    with SessionLocal.begin() as session:
        prior = session.execute(select(CrewRun.id).where(
            CrewRun.source_user == "web-admin",
        ).limit(30)).all()
        if len(prior) >= 30:
            raise ValueError("Web demo run limit reached")
        run_id = str(uuid4())
        session.add(CrewRun(id=run_id, flow=flow, source_user="web-admin",
                            channel_id="web", status="QUEUED"))
        session.flush()
        _new_job(session, run_id=run_id, role="Concierge", kind="dispatch",
                 input_data={"flow": flow})
        record(session, agent="Concierge", action="web_demo_queued", request_id=run_id,
               detail={"flow": flow})
    return run_id


def get_run(run_id: str) -> dict | None:
    with SessionLocal() as session:
        run = session.get(CrewRun, run_id)
        if run is None:
            return None
        jobs = session.execute(select(AgentJob).where(AgentJob.run_id == run_id)
                               .order_by(AgentJob.created_at, AgentJob.id)).scalars().all()
        return {
            "id": run.id, "flow": run.flow, "source_user": run.source_user,
            "channel_id": run.channel_id, "status": run.status,
            "created_at": run.created_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "jobs": [{
                "id": job.id, "role": job.role, "kind": job.kind, "status": job.status,
                "attempts": job.attempts, "output": json.loads(job.output_json or "{}"),
                "error": job.error,
            } for job in jobs],
        }


def _approval_job(session: Session, run_id: str) -> tuple[CrewRun | None, AgentJob | None]:
    run = session.execute(select(CrewRun).where(CrewRun.id == run_id).with_for_update()).scalar_one_or_none()
    if run is None or run.flow not in {"luma-event", "eventbrite-event"}:
        return run, None
    job = session.execute(select(AgentJob).where(
        AgentJob.run_id == run_id,
        AgentJob.kind == ("luma_publish" if run.flow == "luma-event" else "eventbrite_publish"),
        AgentJob.status == "WAITING_APPROVAL",
    ).with_for_update()).scalar_one_or_none()
    return run, job


def review_luma_run(run_id: str, *, user_id: str) -> str:
    if not _admin_user(user_id):
        return "Only an approved Pengwin admin can review this event draft."
    with SessionLocal() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None:
            return "I can't find an event draft waiting for approval under that reference."
        plan = LumaPlan.model_validate(json.loads(job.input_json)["plan"])
        return (eventbrite_preview if run.flow == "eventbrite-event" else approval_preview)(run_id, plan)


def approve_luma_run(run_id: str, snapshot: str, *, user_id: str) -> str:
    if not _admin_user(user_id):
        return "Only an approved Pengwin admin can publish an event."
    with SessionLocal.begin() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None or run.status != "WAITING_APPROVAL":
            return "I can't find an event draft waiting for approval under that reference."
        if run.flow == "luma-event" and os.getenv("LUMA_ENABLED", "false").lower() != "true":
            return "I can't publish to Luma yet. This draft is still waiting; the calendar needs Plus access."
        if run.flow == "eventbrite-event" and os.getenv("EVENTBRITE_ENABLED", "false").lower() != "true":
            return "I can't publish to Eventbrite yet. This draft is still waiting while we fix the organizer connection."
        data = json.loads(job.input_json)
        if data["snapshot"] != snapshot:
            return "That approval code doesn't match the current draft. Use /crew review <reference> to see it again."
        plan = LumaPlan.model_validate(data["plan"])
        if plan.start_at <= datetime.now(timezone.utc):
            return "That event time has passed. Send me a new date and I'll draft it again."
        data["approved_by"] = user_id
        data["approved_snapshot"] = snapshot
        job.input_json = json.dumps(data, sort_keys=True)
        job.status = "QUEUED"
        job.updated_at = datetime.now(timezone.utc)
        run.status = "RUNNING"
        record(session, agent="Control", action="event_approved", request_id=run_id,
               detail={"approver": user_id, "snapshot": snapshot})
    return "Approved. Events is creating it now. I'll post the result here."


def reject_luma_run(run_id: str, *, user_id: str) -> str:
    if not _admin_user(user_id):
        return "Only an approved Pengwin admin can cancel this event draft."
    with SessionLocal.begin() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None or run.status != "WAITING_APPROVAL":
            return "I can't find an event draft waiting for approval under that reference."
        job.status = "HELD"
        job.updated_at = datetime.now(timezone.utc)
        run.status = "REJECTED"
        run.completed_at = datetime.now(timezone.utc)
        record(session, agent="Control", action="event_rejected", request_id=run_id,
               detail={"reviewer": user_id})
    return "Canceled. Nothing was published and no invitations were sent."


def decide_event_in_thread(*, thread_ts: str | None, user_id: str,
                           channel_id: str, action: str) -> str:
    """Bind a plain-English decision to the exact draft shown in that Slack thread."""
    if action not in {"approve", "reject", "review"}:
        raise ValueError("Unsupported event decision")
    if not _admin_user(user_id):
        return "Only an approved Pengwin admin can decide on an event draft."
    if not thread_ts:
        return "Reply inside the event draft's thread so I know which event you mean."
    with SessionLocal() as session:
        waiting = session.execute(select(CrewRun).where(
            CrewRun.source_user == user_id,
            CrewRun.channel_id == channel_id,
            CrewRun.status == "WAITING_APPROVAL",
            CrewRun.flow.in_(("eventbrite-event", "luma-event")),
        )).scalars().all()
        matches = []
        for run in waiting:
            key = "crew_thread_" + hashlib.sha256(run.id.encode()).hexdigest()[:24]
            flag = session.get(ControlFlag, key)
            if flag and flag.value == thread_ts:
                matches.append(run.id)
        if len(matches) != 1:
            return "I can't match this thread to one waiting event draft. Ask Events to show the draft again."
        run_id = matches[0]
        if action == "approve":
            job = session.execute(select(AgentJob).where(
                AgentJob.run_id == run_id,
                AgentJob.status == "WAITING_APPROVAL",
                AgentJob.kind.in_(("eventbrite_publish", "luma_publish")),
            )).scalar_one_or_none()
            if job is None:
                return "That event draft is no longer waiting for approval."
            snapshot = json.loads(job.input_json)["snapshot"]
    if action == "review":
        return review_luma_run(run_id, user_id=user_id)
    if action == "reject":
        return reject_luma_run(run_id, user_id=user_id)
    return approve_luma_run(run_id, snapshot, user_id=user_id)


def claim_next_job(role: str) -> str | None:
    if role not in ROLE_KINDS:
        raise ValueError("Unknown crew role")
    # Compare-and-swap prevents duplicate claims even on SQLite, where
    # SELECT FOR UPDATE is unavailable. Postgres workers may also overlap.
    with SessionLocal.begin() as session:
        candidates = session.execute(
            select(AgentJob.id, AgentJob.run_id, AgentJob.kind).join(CrewRun, AgentJob.run_id == CrewRun.id)
            .where(AgentJob.role == role, AgentJob.status == "QUEUED",
                   CrewRun.status.in_(("QUEUED", "RUNNING")))
            .order_by(AgentJob.created_at, AgentJob.id).limit(10)
        ).all()
        for job_id, run_id, kind in candidates:
            if role == "Treasurer":
                # Serialize payout jobs for one run. If one outcome becomes
                # uncertain, it can hold the run before another payout starts.
                session.execute(
                    select(CrewRun.id).where(CrewRun.id == run_id).with_for_update()
                ).scalar_one()
                other_running = session.execute(
                    select(AgentJob.id).where(
                        AgentJob.run_id == run_id, AgentJob.role == "Treasurer",
                        AgentJob.status == "RUNNING",
                    ).limit(1)
                ).first()
                if other_running:
                    continue
                if kind == "budget_review":
                    buyer_pending = session.execute(select(AgentJob.id).where(
                        AgentJob.run_id == run_id,
                        AgentJob.kind == "product_source",
                        AgentJob.status != "DONE",
                    ).limit(1)).first()
                    if buyer_pending:
                        continue
            claimed = session.execute(
                update(AgentJob).where(
                    AgentJob.id == job_id, AgentJob.status == "QUEUED",
                    AgentJob.run_id.in_(
                        select(CrewRun.id).where(CrewRun.status.in_(("QUEUED", "RUNNING")))
                    ),
                )
                .values(status="RUNNING", attempts=AgentJob.attempts + 1,
                        updated_at=datetime.now(timezone.utc))
            )
            if claimed.rowcount == 1:
                return job_id
    return None


def _refresh_run(session: Session, run_id: str) -> None:
    run = session.get(CrewRun, run_id)
    session.flush()
    statuses = session.execute(select(AgentJob.status).where(AgentJob.run_id == run_id)).scalars().all()
    if "FAILED" in statuses:
        run.status = "FAILED"
    elif "HELD" in statuses:
        run.status = "HELD"
    elif "WAITING_APPROVAL" in statuses:
        run.status = "WAITING_APPROVAL"
    elif any(status in {"QUEUED", "RUNNING"} for status in statuses):
        run.status = "RUNNING"
    else:
        run.status = "COMPLETE"
    if run.status in {"FAILED", "HELD", "COMPLETE"}:
        run.completed_at = datetime.now(timezone.utc)


def _stop_run(session: Session, run_id: str, *, status: str) -> None:
    session.execute(
        update(AgentJob).where(AgentJob.run_id == run_id, AgentJob.status == "QUEUED")
        .values(status="HELD", updated_at=datetime.now(timezone.utc))
    )
    run = session.get(CrewRun, run_id)
    run.status = status
    run.completed_at = datetime.now(timezone.utc)


def _admin_user(user_id: str) -> bool:
    return bool(user_id) and user_id in {
        part.strip() for part in os.getenv("SLACK_ADMIN_USER_IDS", "").split(",") if part.strip()
    }


def _money(amount_cents: int, currency: str) -> str:
    symbol = "€" if currency == "EUR" else "$" if currency == "USD" else currency + " "
    return f"{symbol}{amount_cents / 100:,.2f}"


def _item_phrase(sku: str, qty: int) -> str:
    singular, plural = ITEM_LABELS[sku]
    return f"{qty} {singular if qty == 1 else plural}"


_MONTHS = {name: index for index, names in enumerate((
    ("jan", "january"), ("feb", "february"), ("mar", "march"),
    ("apr", "april"), ("may",), ("jun", "june"), ("jul", "july"),
    ("aug", "august"), ("sep", "sept", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december"),
), 1) for name in names}


def _asked_month_day(text: str) -> tuple[int, int] | None:
    month_names = "|".join(sorted(_MONTHS, key=len, reverse=True))
    named = re.search(rf"\b({month_names})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text, re.I)
    if named:
        return _MONTHS[named.group(1).lower()], int(named.group(2))
    numeric = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/\d{2,4})?\b", text)
    if numeric:
        month, day = int(numeric.group(1)), int(numeric.group(2))
        if 1 <= month <= 12 and 1 <= day <= 31:
            return month, day
    iso = re.search(r"\b\d{4}-(\d{2})-(\d{2})\b", text)
    if iso:
        return int(iso.group(1)), int(iso.group(2))
    return None


def _eventbrite_status_reply(run: CrewRun, question: str) -> tuple[dict, str]:
    """Read saved Pengwin publications and, where possible, Eventbrite now."""
    from .luma import LumaPlan
    from .memory import link_run_resource

    requested_date = _asked_month_day(question)
    with SessionLocal() as session:
        completed = session.execute(select(AgentJob).join(CrewRun, AgentJob.run_id == CrewRun.id).where(
            AgentJob.kind == "eventbrite_publish", AgentJob.status == "DONE",
            CrewRun.channel_id == run.channel_id,
            CrewRun.source_user == run.source_user,
        ).order_by(AgentJob.created_at.desc()).limit(20)).scalars().all()
    matches = []
    for published in completed:
        try:
            saved_input = json.loads(published.input_json)
            saved_output = json.loads(published.output_json)
            plan = LumaPlan.model_validate(saved_input["plan"])
            event_id = saved_output["event_id"]
            if not isinstance(event_id, str) or not event_id.isdecimal():
                continue
            if requested_date and (plan.start_at.month, plan.start_at.day) != requested_date:
                continue
            matches.append((plan, event_id, saved_output.get("url")))
        except (ValueError, KeyError, TypeError):
            continue
    if not matches:
        date_words = " for that date" if requested_date else ""
        return {"count": 0}, (f"I don't see a saved Pengwin Eventbrite publication{date_words}. "
                                "I haven't created another event.")
    if len(matches) > 5:
        return {"count": len(matches), "ambiguous": True}, (
            "I found several Pengwin publications. Which event title or date should I check?"
        )
    lines = []
    records = []
    for plan, event_id, saved_url in matches:
        current = None
        try:
            current = EventbriteClient().read_status(event_id)
        except Exception:
            pass
        checked_at = datetime.now(timezone.utc).isoformat() if current else None
        url = (current or {}).get("url") or saved_url
        if not isinstance(url, str) or not url.startswith("https://www.eventbrite.com/"):
            url = None
        date_label = f"{plan.start_at:%b} {plan.start_at.day}, {plan.start_at.year}"
        if current is None:
            state = "published by Pengwin; current status not checked"
        elif current["status"] == "live":
            state = f"Eventbrite confirms it is live now (checked {checked_at[:16]} UTC)"
            if current.get("listed") is False:
                state += " (unlisted)"
        else:
            state = f"Eventbrite says {current['status']} (checked {checked_at[:16]} UTC)"
        lines.append(f"{plan.name} — {date_label}: {state}." + (f" {url}" if url else ""))
        safe_facts = {"title": plan.name, "date": plan.start_at.date().isoformat(),
                      "status": (current or {}).get("status", "unchecked"),
                      "checked_at": checked_at, "url": url}
        link_run_resource(run_id=run.id, role="Events", resource_type="eventbrite_event",
                          resource_id=event_id, safe_facts=safe_facts)
        records.append(safe_facts)
    return {"events": records}, "\n".join(lines)


def _supersede_prior_handoffs(project_id: str, *, current_run_id: str) -> None:
    """A new project revision cannot reuse an older purchase approval."""
    with SessionLocal.begin() as session:
        prior_ids = select(ProjectRunLink.run_id).where(
            ProjectRunLink.project_id == project_id,
            ProjectRunLink.run_id != current_run_id,
        )
        handoffs = session.execute(select(RunResourceLink).where(
            RunResourceLink.run_id.in_(prior_ids),
            RunResourceLink.resource_kind == "checkout_handoff",
        )).scalars().all()
        for handoff in handoffs:
            facts = json.loads(handoff.facts_json)
            if facts.get("status") != "SUPERSEDED":
                facts["status"] = "SUPERSEDED"
                facts["superseded_by_run"] = current_run_id
                handoff.facts_json = json.dumps(facts, sort_keys=True)
                record(session, agent="Buyer", action="checkout_handoff_superseded",
                       request_id=handoff.run_id, detail={"project_revision": current_run_id})


def _project_event_work(run: CrewRun, plan_data: dict) -> tuple[dict, str]:
    from .project_planner import ProjectPlan
    from .research import lookup_project_facts

    plan = ProjectPlan.model_validate(plan_data)
    event = plan.event
    title = event.title if event else plan.name
    venue = event.venue_name if event else None
    date_phrase = event.date_phrase if event else None
    time_phrase = event.time_phrase if event else None
    audience = plan.invitations.audience_phrase if plan.invitations else None
    options: list[str] = []
    if date_phrase and re.search(r"\b(?:about|around|roughly|approximately|next)\b.*\bmonth\b|\bin a month\b", date_phrase, re.I):
        local_day = datetime.now(ZoneInfo("America/Los_Angeles")).date() + timedelta(days=30)
        options = [(local_day + timedelta(days=offset)).isoformat() for offset in (0, 1, 2)]
    description = (
        f"Join us for {title} at {venue or '[venue to confirm]'}. "
        f"The date is {date_phrase or '[date to confirm]'} and the time is "
        f"{time_phrase or '[time to confirm]'}. "
        "We will share the final details after the venue and registration page are approved."
    )
    invite_copy = (
        f"You're invited to {title}. We are planning it for {date_phrase or '[date]'} "
        f"at {venue or '[venue]'}. Reply if you would like the RSVP details once confirmed."
    )
    venue_search = None
    route_search = None
    official_route = None
    if venue and re.fullmatch(r"(?:the\s+)?salesforce park", venue.strip(), re.I):
        # TJPA's own permits page was checked on 2026-09-27. It separates
        # ticketed/private events, simple group outings, and public activations.
        # This is an inquiry route, never evidence of availability or a booking.
        official_route = {
            "operator": "Transbay Joint Powers Authority",
            "url": "https://www.tjpa.org/permits-reservations",
            "checked_at": "2026-09-27",
            "availability": "UNCHECKED",
        }
    if venue:
        venue_search = lookup_project_facts(f"{venue} event venue", kind="place", max_results=2)
        route_search = lookup_project_facts(f"{venue} official event reservation permit", kind="web", max_results=2)
    lines = [f"Event plan: {title}. This plan has not reserved a venue or published a new Eventbrite page."]
    if options:
        lines.append("Date options to confirm: " + ", ".join(options) + ".")
    elif date_phrase:
        lines.append(f"Requested timing: {date_phrase}; please confirm an exact calendar date.")
    else:
        lines.append("I need an event date.")
    if not time_phrase:
        lines.append("I also need a start time and expected duration.")
    if event and event.capacity is None:
        lines.append("I need an RSVP capacity before drafting a publishable Eventbrite page.")
    if venue_search and venue_search.status == "ok" and venue_search.results:
        leads = [f"{lead.title}: {lead.url}" for lead in venue_search.results if lead.url]
        if leads:
            lines.append("Venue search leads (availability unverified): " + " | ".join(leads[:2]))
    if route_search and route_search.status == "ok" and route_search.results:
        leads = [f"{lead.title}: {lead.url}" for lead in route_search.results if lead.url]
        if leads:
            lines.append("Possible reservation routes to verify with the venue: " + " | ".join(leads[:2]))
    if official_route:
        lines.append(
            "Official Salesforce Park inquiry route (TJPA; reviewed 2026-09-27): "
            + official_route["url"]
            + ". TJPA distinguishes ticketed/private events, group outings, and public "
              "activations; confirm the right permit and availability with the operator. "
              "No reservation has been requested."
        )
    if venue_search and venue_search.status != "ok":
        lines.append(
            "Live venue search is unavailable; availability is not checked."
            if official_route else
            "Live venue search is unavailable; please share the park's city or official booking page."
        )
    elif not venue:
        lines.append("Which venue and city should I research?")
    lines.append("Draft description: " + description)
    if plan.invitations:
        lines.append("Target audience: " + (audience or "please name the group you want to invite") + ".")
        lines.append("Draft invitation: " + invite_copy)
        lines.append("No mailing list was imported and no invitations were sent.")
    output = {
        "venue_status": "UNCONFIRMED", "eventbrite_status": "NOT_CREATED",
        "invitation_status": "DRAFT_ONLY", "date_options": options,
        "description_draft": description,
        "invitation_draft": invite_copy if plan.invitations else None,
        "research_status": (venue_search.status if venue_search else "not_requested"),
        "official_reservation_route": official_route,
    }
    return output, "\n".join(lines)[:3000]


def _project_product_work(run: CrewRun, plan_data: dict) -> tuple[dict, str]:
    from .memory import link_run_resource
    from .project_planner import ProjectPlan
    from .research import check_printful_water_bottle_prices, lookup_project_facts

    plan = ProjectPlan.model_validate(plan_data)
    if plan.swag is None:
        return {"status": "NOT_REQUESTED"}, "No product sourcing was requested.",
    quantity = plan.swag.quantity
    search = lookup_project_facts("custom water bottles event bulk order", kind="web", max_results=2)
    fact = check_printful_water_bottle_prices()
    output: dict = {
        "status": "RESEARCHED", "product": "water_bottle", "quantity": quantity,
        "publisher_status": fact.status, "search_status": search.status,
        "source_url": fact.source_url,
        "price_kind": "PRODUCT_RANGE_ESTIMATE" if fact.status == "ok" else "UNAVAILABLE",
        "currency": fact.currency, "unit_min": fact.min_price,
        "unit_max": fact.max_price,
        "checked_at": fact.checked_at.isoformat() if fact.checked_at else None,
        "subtotal_min": round(quantity * fact.min_price, 2) if quantity and fact.min_price is not None else None,
        "subtotal_max": round(quantity * fact.max_price, 2) if quantity and fact.max_price is not None else None,
        "checkout_status": "NOT_READY",
    }
    lines = ["Buyer sourced custom water bottles; no order or payment was placed."]
    if fact.status == "ok":
        lines.append(f"Printful's official product range is ${fact.min_price:.2f}–${fact.max_price:.2f} per bottle, checked {fact.checked_at:%Y-%m-%d %H:%M} UTC: {fact.source_url}")
        if quantity:
            lines.append(f"For {quantity}, product subtotal estimate: ${output['subtotal_min']:.2f}–${output['subtotal_max']:.2f}.")
        lines.append("Shipping, tax, artwork, variant, stock, and the final checkout total are not quoted.")
        link_run_resource(run_id=run.id, role="Buyer", resource_type="publisher_product",
                          resource_id="printful-water-bottles", safe_facts={
                              "source_url": fact.source_url, "checked_at": output["checked_at"],
                              "currency": fact.currency, "unit_min": fact.min_price,
                              "unit_max": fact.max_price, "price_kind": output["price_kind"],
                          })
    else:
        lines.append("The official product price could not be checked, so I have no current estimate.")
    if search.status == "ok" and search.results:
        leads = [f"{lead.title}: {lead.url}" for lead in search.results if lead.url]
        if leads:
            lines.append("Other search leads (not verified quotes): " + " | ".join(leads[:2]))
    elif search.status != "ok":
        lines.append("Broader live product search is unavailable.")
    missing = []
    if quantity is None:
        missing.append("quantity")
    if not plan.swag.design_phrase:
        missing.append("print-ready artwork")
    missing.extend(("exact product variant", "delivery destination", "landed checkout total"))
    lines.append("For a checkout handoff, I still need " + ", ".join(missing) + ".")
    lines.append("A later exact handoff will need fresh Slack approval if the quantity or price changes.")
    return output, "\n".join(lines)[:3000]


def _project_budget_work(run: CrewRun, plan_data: dict) -> tuple[dict, str]:
    from .memory import link_run_resource
    from .project_planner import ProjectPlan

    plan = ProjectPlan.model_validate(plan_data)
    reason = f"Water bottles and event planning for {plan.event.title if plan.event else plan.name}"
    with SessionLocal() as session:
        budget = session.execute(select(Budget).where(
            Budget.office_id == "SF", Budget.category == "swag",
        )).scalar_one_or_none()
        sourced = session.execute(select(AgentJob).where(
            AgentJob.run_id == run.id,
            AgentJob.kind == "product_source", AgentJob.status == "DONE",
        ).order_by(AgentJob.created_at.desc()).limit(1)).scalar_one_or_none()
        source_output = json.loads(sourced.output_json) if sourced else {}
    room_cents = max(0, budget.limit_cents - budget.spent_cents - budget.reserved_cents) if budget else None
    low = source_output.get("subtotal_min")
    high = source_output.get("subtotal_max")
    over_budget = bool(room_cents is not None and high is not None and high * 100 > room_cents)
    output = {
        "reason": reason, "review_status": "ESTIMATE_ONLY", "product_subtotal_min": low,
        "product_subtotal_max": high, "currency": "USD" if high is not None else None,
        "internal_demo_budget_room_cents": room_cents, "over_internal_budget": over_budget,
        "payment_status": "NONE", "reserved_cents": 0,
    }
    lines = [f"Treasurer recorded the purpose: {reason}."]
    if high is not None:
        lines.append(f"The current product-only estimate is ${low:.2f}–${high:.2f}; shipping and tax remain unknown.")
        if over_budget:
            lines.append("The high end exceeds the internal demo swag allocation. We need a revised plan.")
        else:
            lines.append("The product estimate fits the internal demo swag allocation, but this is not checkout approval.")
    else:
        lines.append("I cannot assess an amount until Buyer has a current quote and quantity.")
    lines.append("No funds were reserved, no sandbox transfer was submitted, and no real payment was made.")
    link_run_resource(run_id=run.id, role="Treasurer", resource_type="budget_review",
                      resource_id="swag-review", safe_facts=output)
    return output, "\n".join(lines)[:2000]


def _perform(job: AgentJob, run: CrewRun) -> tuple[dict, list[tuple[str, str, dict]], str, bool]:
    if job.kind not in ROLE_KINDS.get(job.role, set()):
        raise ValueError("Job kind exceeds role capability")
    data = json.loads(job.input_json)
    if job.role == "Concierge":
        if run.flow == "event-status":
            return {}, [("Events", "eventbrite_status", {
                "question": data.get("text", ""), "context": data.get("context", "")
            })], "Events is checking our saved publications and their current provider status.", False
        if run.flow == "project-plan":
            from .memory import create_or_update_project
            from .project_planner import checked_project_plan, project_plan_summary
            try:
                request_text = data["text"]
                context = data.get("context", "")
                plan = checked_project_plan(
                    VultrInference().project_plan(request_text=request_text, context=context),
                    request_text, context=context,
                )
            except (KeyError, ValueError) as exc:
                question = ("I can coordinate the event, water bottles, and invitations. "
                            "Please give the event location and approximate date, plus any bottle quantity or audience you know. "
                            "Nothing has been booked, bought, or sent.")
                return {"clarification": str(exc)[:160]}, [], question, False
            except Exception as exc:
                message = ("I couldn't build a checked project plan right now. "
                           "Please try again shortly; nothing was booked, bought, or sent.")
                return {"unavailable": type(exc).__name__}, [], message, False
            plan_data = plan.model_dump(mode="json")
            project_id = create_or_update_project(
                run_id=run.id, owner_user_id=run.source_user,
                channel_id=run.channel_id, thread_root_ts=data.get("thread_root_ts"),
                name=plan.name, safe_plan=plan_data, status="ACTIVE",
            )
            if project_id != run.id:
                _supersede_prior_handoffs(project_id, current_run_id=run.id)
            successors: list[tuple[str, str, dict]] = []
            if plan.event is not None or plan.invitations is not None:
                successors.append(("Events", "event_research", {"plan": plan_data}))
            if plan.swag is not None:
                successors.append(("Buyer", "product_source", {"plan": plan_data}))
            successors.append(("Treasurer", "budget_review", {"plan": plan_data}))
            return plan_data, successors, (
                project_plan_summary(plan) + "\nEvents, Buyer, and Treasurer are checking their parts now."
            ), False
        if run.flow == "code-task":
            return {}, [("Buyer", "code_execute", {"goal": data["text"]})], (
                "Buyer is running the code in a fresh, isolated workspace. "
                "I'll bring back the actual result."
            ), False
        if run.flow in {"luma-event", "eventbrite-event"}:
            try:
                candidate = VultrInference().luma_event_plan(request_text=data["text"])
                checker = checked_eventbrite_plan if run.flow == "eventbrite-event" else checked_luma_plan
                plan = checker(candidate, data["text"])
            except ValueError as exc:
                return {"clarification": str(exc)}, [], (
                    f"I need one detail before I can draft the event: {exc}. Nothing was published."
                ), False
            if plan.clarification:
                return {"clarification": plan.clarification}, [], (
                    f"{plan.clarification} Nothing was published."
                ), False
            snapshot = plan_snapshot(plan)
            public = run.flow == "eventbrite-event"
            return {"snapshot": snapshot, "guest_count": len(plan.guests)}, [
                ("Events", "eventbrite_publish" if public else "luma_publish",
                 {"plan": plan.model_dump(mode="json"), "snapshot": snapshot})
            ], (eventbrite_preview if public else approval_preview)(run.id, plan), False
        if run.flow == "natural-language":
            try:
                plan = checked_plan(
                    VultrInference().concierge_plan(request_text=data["text"]), data["text"]
                )
            except ValueError:
                question = "Please tell me the item, quantity, and office (Berlin or San Francisco). I haven't placed an order."
                return {"clarification": question}, [], question, False
            if plan.clarification:
                return {"clarification": plan.clarification}, [], (
                    f"{plan.clarification} I haven't placed an order."
                ), False
            steps = [
                ("Buyer", "purchase", {
                    "office": item.office, "sku": item.sku, "qty": item.quantity,
                    "text": data["text"],
                }) for item in plan.items
            ]
            if plan.lunch_headcount is not None:
                steps.append(("Events", "lunch_prepare", {"headcount": plan.lunch_headcount}))
            summary = ", ".join(
                f"{_item_phrase(item.sku, item.quantity)} for "
                f"{'Berlin' if item.office == 'BER' else 'San Francisco'}"
                for item in plan.items
            )
            if plan.lunch_headcount is not None:
                summary += (", " if summary else "") + f"Berlin lunch for {plan.lunch_headcount}"
            return plan.model_dump(), steps, (
                f"Got it — {summary}. I've sent the details to the crew. "
                "Buyer checks the order; Treasurer checks the budget."
            ), False
        steps = FLOW_STEPS[run.flow]
        return {"steps": len(steps)}, list(steps), (
            f"On it — the crew is handling {flow_label(run.flow)}. "
            "I'll keep the updates in this thread."
        ), False
    if job.role == "Events" and job.kind == "eventbrite_status":
        output, message = _eventbrite_status_reply(run, data.get("question", ""))
        return output, [], message, False
    if job.role == "Events" and job.kind == "event_research":
        output, message = _project_event_work(run, data["plan"])
        return output, [], message, False
    if job.role == "Buyer" and job.kind == "product_source":
        output, message = _project_product_work(run, data["plan"])
        return output, [], message, False
    if job.role == "Treasurer" and job.kind == "budget_review":
        output, message = _project_budget_work(run, data["plan"])
        return output, [], message, False
    if job.role == "Buyer" and job.kind == "code_execute":
        if settings.sandbox_mode != "docker":
            raise RuntimeError("Code tasks require the isolated Docker sandbox VM")
        goal = data["goal"]
        planner = VultrInference()
        draft = planner.code_draft(goal=goal)
        attempts = []
        for index in range(2):
            result = sandbox().execute_code(draft.code, {"goal": goal})
            exit_code = result.get("exit_code")
            if not isinstance(exit_code, int):
                raise RuntimeError("Sandbox did not return a valid exit code")
            code_hash = hashlib.sha256(draft.code.encode()).hexdigest()[:12]
            attempts.append({"code": draft.code, "code_hash": code_hash,
                             "exit_code": exit_code, "stdout": str(result.get("stdout", ""))[:10_000],
                             "stderr": str(result.get("stderr", ""))[:4_000]})
            with SessionLocal.begin() as session:
                record(session, agent="Buyer", action="sandbox_code_attempt", request_id=run.id,
                       detail={"attempt": index + 1, "code_hash": code_hash,
                               "exit_code": exit_code})
            if exit_code == 0:
                output = attempts[-1]["stdout"].strip() or "(no stdout)"
                printable = "".join(c for c in output if c.isprintable() or c in "\n\t")[:900]
                return {"attempts": attempts, "result": output}, [], (
                    "Done. I ran the code in a fresh, isolated workspace"
                    + (" and fixed an error on the second try" if index else "")
                    + f". It printed:\n{printable}"
                ), False
            if exit_code == 124:
                return {"attempts": attempts}, [], (
                    "I stopped that code after 10 seconds. The isolated workspace was removed, "
                    "so it couldn't keep running or affect the rest of Pengwin."
                ), True
            if index == 0:
                draft = planner.code_draft(goal=goal, previous_code=draft.code,
                                           stderr=attempts[-1]["stderr"])
        return {"attempts": attempts}, [], (
            "I tried twice in fresh, isolated workspaces. Both attempts failed, so I stopped. "
            "Ask me to try a different approach."
        ), True
    if job.role == "Buyer" and job.kind == "purchase":
        prepared = prepare_purchase(**data, source="slack", source_user=run.source_user)
        message = ("I checked the quote: "
                   f"{_item_phrase(prepared['proposed_sku'], prepared['proposed_qty'])} "
                   f"for {_money(prepared['amount_cents'], prepared['currency'])}. "
                   "Treasurer gets the final word.")
        next_jobs = [("Treasurer", "pay", {"order_id": prepared["first_order_id"],
                                            "request_id": prepared["request_id"]})]
        return prepared, next_jobs, message, False
    if job.role == "Buyer" and job.kind == "requote":
        order_id = requote_purchase(data["request_id"])
        result = {"request_id": data["request_id"], "order_id": order_id}
        next_jobs = [("Treasurer", "pay", {"order_id": order_id, "request_id": data["request_id"]})]
        return result, next_jobs, (
            "Good catch — the first quote didn't match your request. "
            "I rebuilt it with the requested quantity and sent it back for review."
        ), False
    if job.role == "Events" and job.kind == "lunch_prepare":
        prepared = prepare_berlin_lunch(**data, source="slack", source_user=run.source_user)
        next_jobs = [("Treasurer", "pay", {"order_id": prepared["first_order_id"],
                                            "request_id": prepared["request_id"], "lunch": True})]
        return prepared, next_jobs, (
            f"Berlin lunch for {data['headcount']} is penciled in for "
            f"{prepared['date']} at noon on a calendar draft. "
            "The venue hasn't said yes, so keep the victory lap on ice. No table is reserved."
        ), False
    if job.role == "Events" and job.kind == "lunch_complete":
        complete_berlin_lunch(data["request_id"], data["payment_status"])
        return {"request_id": data["request_id"], "payment_status": data["payment_status"]}, [], (
            "The lunch invite is ready. The budget step is recorded, "
            "but no restaurant has confirmed a reservation."
        ), False
    if job.role == "Events" and job.kind == "luma_publish":
        if not data.get("approved_by") or data.get("approved_snapshot") != data.get("snapshot"):
            raise ValueError("Luma draft has no matching human approval")
        plan = LumaPlan.model_validate(data["plan"])
        client = LumaClient()
        event_id = client.create_event(plan)
        # Persist the provider ID before inviting anyone. If invitation delivery
        # fails or times out, this run is held for reconciliation, not replayed.
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job.id)
            persisted.output_json = json.dumps({"event_id": event_id, "invites": "PENDING"})
            record(session, agent="Events", action="luma_event_created", request_id=run.id,
                   detail={"event_id": event_id, "guest_count": len(plan.guests)})
        skipped = client.send_invites(event_id, plan.guests)
        return {"event_id": event_id, "guest_count": len(plan.guests),
                "skipped_count": len(skipped)}, [], (
            f"{plan.name} is on the calendar. I asked Luma to invite {len(plan.guests)} people"
            + (f"; it skipped {len(skipped)}" if skipped else "")
            + ". Check the guest list to confirm delivery."
        ), False
    if job.role == "Events" and job.kind == "eventbrite_publish":
        if not data.get("approved_by") or data.get("approved_snapshot") != data.get("snapshot"):
            raise ValueError("Eventbrite draft has no matching human approval")
        plan = LumaPlan.model_validate(data["plan"])
        client = EventbriteClient()
        event_id = client.create_draft(plan)
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job.id)
            persisted.output_json = json.dumps({"event_id": event_id, "stage": "DRAFT"})
            record(session, agent="Events", action="eventbrite_draft_created", request_id=run.id,
                   detail={"event_id": event_id, "capacity": plan.capacity})
        ticket_id = client.create_free_ticket(event_id, plan.capacity)
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job.id)
            persisted.output_json = json.dumps({"event_id": event_id, "ticket_id": ticket_id,
                                                "stage": "TICKET_CREATED"})
            record(session, agent="Events", action="eventbrite_free_ticket_created", request_id=run.id,
                   detail={"event_id": event_id, "ticket_id": ticket_id})
        url = client.publish(event_id)
        return {"event_id": event_id, "ticket_id": ticket_id, "url": url}, [], published_message(plan, url), False
    if job.role == "Treasurer" and job.kind == "pay":
        payment = pay_pending_order(data["order_id"], is_admin=_admin_user(run.source_user))
        result = {"request_id": data["request_id"], "order_id": data["order_id"],
                  "payment_id": payment.id, "payment_status": payment.status,
                  "blocked_rule": payment.blocked_rule}
        if payment.status in UNCERTAIN_PAYMENT_STATUSES:
            return result, [], ("I can't confirm whether that payment request went through. "
                                "I've stopped here and won't send it again until we check with Airwallex."), True
        next_jobs = []
        if data.get("lunch"):
            next_jobs.append(("Events", "lunch_complete", {
                "request_id": data["request_id"], "payment_status": payment.status}))
        elif payment.status == "BLOCKED" and payment.blocked_rule in {
            "QUANTITY_SANITY", "SKU_MISMATCH", "VENDOR_ALLOWLIST"
        }:
            with SessionLocal() as session:
                order = session.get(PendingOrder, data["order_id"])
                request = session.get(Request, data["request_id"])
                mismatch = bool(order and request and
                                (order.sku != request.sku or order.qty != request.requested_qty))
            if mismatch:
                next_jobs.append(("Buyer", "requote", {"request_id": data["request_id"]}))
        if payment.status == "SUBMITTED_SANDBOX":
            message = ("Budget approved. Airwallex accepted a test transfer for "
                       f"{_money(payment.amount_cents, payment.currency)}. "
                       "Airwallex has it; settlement is not confirmed.")
        elif payment.status == "SIMULATED":
            message = ("The amount fits the demo budget. I recorded "
                       f"{_money(payment.amount_cents, payment.currency)} for this checkout. "
                       "No real card was charged.")
        elif next_jobs and next_jobs[0][1] == "requote":
            message = "That quote didn't match your request. Buyer is correcting it before anything moves forward."
        else:
            reasons = {
                "BALANCE": "There isn't enough room in the budget.",
                "BENEFICIARY_MISSING": "The payment destination isn't set up.",
                "QUANTITY_SANITY": "The quantity needs another look.",
                "SKU_MISMATCH": "The item doesn't match your request.",
                "VENDOR_ALLOWLIST": "That vendor isn't approved for this workspace.",
            }
            message = reasons.get(payment.blocked_rule, "I couldn't clear that payment. Nothing was charged.")
        return result, next_jobs, message, False
    raise ValueError("Unsupported job")


def _notify(run_id: str, role: str, message: str) -> None:
    key = "crew_thread_" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
    with SessionLocal() as session:
        target = session.get(CrewRun, run_id)
        if target and target.channel_id == "web":
            return
    try:
        with SessionLocal() as session:
            run = session.get(CrewRun, run_id)
            existing = session.get(ControlFlag, key)
            channel_id = run.channel_id
            thread_ts = existing.value if existing else None
            if not thread_ts:
                inbound = session.execute(select(ConversationTurn.thread_root_ts).where(
                    ConversationTurn.run_id == run_id,
                    ConversationTurn.direction == "in",
                ).order_by(ConversationTurn.created_at.desc()).limit(1)).scalar_one_or_none()
                thread_ts = inbound or None
        # Keep identifiers in the durable run record, not at the front of chat.
        prefix = f"Run {run_id}: "
        ts = post_role_update(role, channel_id,
                              message.removeprefix(prefix), thread_ts=thread_ts)
        if role == "Concierge" and not existing:
            with SessionLocal.begin() as session:
                session.add(ControlFlag(key=key, value=thread_ts or ts))
    except Exception as exc:
        # A Slack outage does not replay a completed purchase. The run remains
        # queryable via its durable job and audit records.
        with SessionLocal.begin() as session:
            record(session, agent=role, action="slack_update_failed", request_id=run_id,
                   detail={"error_type": type(exc).__name__}, severity="error")
def run_one_job(role: str) -> bool:
    """Process one queued job for this role. Returns False when no job is ready."""
    job_id = claim_next_job(role)
    if job_id is None:
        return False
    with SessionLocal() as session:
        job = session.get(AgentJob, job_id)
        run = session.get(CrewRun, job.run_id)
        run_id = run.id
        # Detach only the immutable fields needed by the role handler.
        session.expunge(job)
        session.expunge(run)
    try:
        output, successors, message, hold = _perform(job, run)
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job_id)
            if persisted.status != "RUNNING":
                raise RuntimeError("Claimed job state changed")
            persisted.output_json = json.dumps(output, sort_keys=True)
            persisted.status = "HELD" if hold else "DONE"
            persisted.updated_at = datetime.now(timezone.utc)
            if hold:
                _stop_run(session, run_id, status="HELD")
            else:
                for next_role, kind, input_data in successors:
                    next_job = _new_job(session, run_id=run_id, role=next_role, kind=kind,
                                        input_data=input_data)
                    if kind in {"luma_publish", "eventbrite_publish"}:
                        next_job.status = "WAITING_APPROVAL"
                _refresh_run(session, run_id)
            record(session, agent=role, action="agent_job_held" if hold else "agent_job_done",
                   request_id=run_id, detail={"job_id": job_id, "kind": job.kind,
                                              "next_jobs": len(successors)}, severity="warning" if hold else "info")
        _notify(run_id, role, message)
    except Exception as exc:
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job_id)
            if persisted and persisted.status == "RUNNING":
                provider_mutation = role == "Treasurer" or persisted.kind in {
                    "luma_publish", "eventbrite_publish",
                }
                persisted.status = "HELD" if provider_mutation else "FAILED"
                persisted.error = type(exc).__name__
                persisted.updated_at = datetime.now(timezone.utc)
                _stop_run(session, run_id, status="HELD" if provider_mutation else "FAILED")
                record(session, agent=role, action="agent_job_failed", request_id=run_id,
                       detail={"job_id": job_id, "kind": job.kind,
                               "error_type": type(exc).__name__}, severity="error")
        _notify(run_id, role, "I hit a problem and stopped safely. Please check this request before trying again.")
    return True
