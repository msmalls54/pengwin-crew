"""Durable, role-routed office workflows.

Only queued jobs are claimable. A claimed job is never retried automatically:
after a crash it must be inspected before a person decides whether it is safe
to resume. This is especially important around payment submission.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .audit import record
from .config import settings
from .db import AgentJob, ControlFlag, CrewRun, PendingOrder, Request, SessionLocal
from .eventbrite import EventbriteClient, approval_preview as eventbrite_preview, checked_eventbrite_plan
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
    "luma-event": "a Luma event draft",
    "eventbrite-event": "a public RSVP event draft",
    "code-task": "a sandboxed code task",
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
    "Buyer": {"purchase", "requote", "code_execute"},
    "Events": {"lunch_prepare", "lunch_complete", "luma_publish", "eventbrite_publish"},
    "Treasurer": {"pay"},
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
               request_text: str | None = None) -> str:
    if flow not in FLOW_STEPS and flow not in {"natural-language", "luma-event", "eventbrite-event", "code-task"}:
        raise ValueError("Unknown crew flow")
    if flow in {"natural-language", "luma-event", "eventbrite-event", "code-task"}:
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
        return "Only a configured crew admin can review event details."
    with SessionLocal() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None:
            return "No event draft is waiting for approval under that run ID."
        plan = LumaPlan.model_validate(json.loads(job.input_json)["plan"])
        return (eventbrite_preview if run.flow == "eventbrite-event" else approval_preview)(run_id, plan)


def approve_luma_run(run_id: str, snapshot: str, *, user_id: str) -> str:
    if not _admin_user(user_id):
        return "Only a configured crew admin can approve an event."
    with SessionLocal.begin() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None or run.status != "WAITING_APPROVAL":
            return "No event draft is waiting for approval under that run ID."
        if run.flow == "luma-event" and os.getenv("LUMA_ENABLED", "false").lower() != "true":
            return "Luma publishing is not ready. The draft remains waiting; configure a Plus calendar key first."
        if run.flow == "eventbrite-event" and os.getenv("EVENTBRITE_ENABLED", "false").lower() != "true":
            return "Eventbrite publishing is not ready. The draft remains waiting; configure its organizer credentials first."
        data = json.loads(job.input_json)
        if data["snapshot"] != snapshot:
            return "The draft snapshot does not match. Use /crew review <run ID> before approving."
        plan = LumaPlan.model_validate(data["plan"])
        if plan.start_at <= datetime.now(timezone.utc):
            return "The event start time has passed. Submit a fresh request."
        data["approved_by"] = user_id
        data["approved_snapshot"] = snapshot
        job.input_json = json.dumps(data, sort_keys=True)
        job.status = "QUEUED"
        job.updated_at = datetime.now(timezone.utc)
        run.status = "RUNNING"
        record(session, agent="Control", action="event_approved", request_id=run_id,
               detail={"approver": user_id, "snapshot": snapshot})
    provider = "Eventbrite" if run.flow == "eventbrite-event" else "Luma"
    return f"Approved draft {snapshot}. Events is creating the {provider} event; no duplicate submission will be retried automatically."


def reject_luma_run(run_id: str, *, user_id: str) -> str:
    if not _admin_user(user_id):
        return "Only a configured crew admin can reject an event."
    with SessionLocal.begin() as session:
        run, job = _approval_job(session, run_id)
        if run is None or job is None or run.status != "WAITING_APPROVAL":
            return "No event draft is waiting for approval under that run ID."
        job.status = "HELD"
        job.updated_at = datetime.now(timezone.utc)
        run.status = "REJECTED"
        run.completed_at = datetime.now(timezone.utc)
        record(session, agent="Control", action="event_rejected", request_id=run_id,
               detail={"reviewer": user_id})
    return "Event draft rejected. No event was published or invitation sent."


def claim_next_job(role: str) -> str | None:
    if role not in ROLE_KINDS:
        raise ValueError("Unknown crew role")
    # Compare-and-swap prevents duplicate claims even on SQLite, where
    # SELECT FOR UPDATE is unavailable. Postgres workers may also overlap.
    with SessionLocal.begin() as session:
        candidates = session.execute(
            select(AgentJob.id, AgentJob.run_id).join(CrewRun, AgentJob.run_id == CrewRun.id)
            .where(AgentJob.role == role, AgentJob.status == "QUEUED",
                   CrewRun.status.in_(("QUEUED", "RUNNING")))
            .order_by(AgentJob.created_at, AgentJob.id).limit(10)
        ).all()
        for job_id, run_id in candidates:
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


def _perform(job: AgentJob, run: CrewRun) -> tuple[dict, list[tuple[str, str, dict]], str, bool]:
    if job.kind not in ROLE_KINDS.get(job.role, set()):
        raise ValueError("Job kind exceeds role capability")
    data = json.loads(job.input_json)
    if job.role == "Concierge":
        if run.flow == "code-task":
            return {}, [("Buyer", "code_execute", {"goal": data["text"]})], (
                f"Run {run.id}: Buyer is running this in a disposable, offline code container. "
                "I’ll report what actually ran and what it printed."
            ), False
        if run.flow in {"luma-event", "eventbrite-event"}:
            try:
                candidate = VultrInference().luma_event_plan(request_text=data["text"])
                checker = checked_eventbrite_plan if run.flow == "eventbrite-event" else checked_luma_plan
                plan = checker(candidate, data["text"])
            except ValueError as exc:
                return {"clarification": str(exc)}, [], (
                    f"Run {run.id}: {exc}. No event was published or invitation sent."
                ), False
            if plan.clarification:
                return {"clarification": plan.clarification}, [], (
                    f"Run {run.id}: {plan.clarification} No event was published or invitation sent."
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
                return {"clarification": question}, [], f"Run {run.id}: Tiny paperwork snag: {question}", False
            if plan.clarification:
                return {"clarification": plan.clarification}, [], (
                    f"Run {run.id}: {plan.clarification} I haven't placed an order."
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
                f"Run {run.id}: Got it — {summary}. I've sent the details to the crew. "
                "Buyer checks the order; Treasurer checks the budget."
            ), False
        steps = FLOW_STEPS[run.flow]
        return {"steps": len(steps)}, list(steps), (
            f"Run {run.id}: On it — {flow_label(run.flow)} is split into {len(steps)} tasks. "
            "I'm keeping the chaos in one thread."
        ), False
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
                    f"Run {run.id}: Buyer executed Python in the offline sandbox. "
                    f"Exit 0 after {index + 1} attempt(s); code hash {code_hash}. "
                    f"Actual output: {printable}"
                ), False
            if index == 0:
                draft = planner.code_draft(goal=goal, previous_code=draft.code,
                                           stderr=attempts[-1]["stderr"])
        return {"attempts": attempts}, [], (
            f"Run {run.id}: Buyer ran the code twice in fresh sandboxes. Both attempts failed; "
            "the run is held for review. No provider action was made."
        ), True
    if job.role == "Buyer" and job.kind == "purchase":
        prepared = prepare_purchase(**data, source="slack", source_user=run.source_user)
        message = (f"Run {run.id}: I checked the numbers: "
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
            f"Run {run.id}: Good catch — the first quote didn't match the request. "
            "I rebuilt it with the requested quantity and sent it back for review."
        ), False
    if job.role == "Events" and job.kind == "lunch_prepare":
        prepared = prepare_berlin_lunch(**data, source="slack", source_user=run.source_user)
        next_jobs = [("Treasurer", "pay", {"order_id": prepared["first_order_id"],
                                            "request_id": prepared["request_id"], "lunch": True})]
        return prepared, next_jobs, (
            f"Run {run.id}: Berlin lunch for {data['headcount']} is penciled in for "
            f"{prepared['date']} at noon on a calendar draft. "
            "The venue hasn't said yes, so keep the victory lap on ice. No table is reserved."
        ), False
    if job.role == "Events" and job.kind == "lunch_complete":
        complete_berlin_lunch(data["request_id"], data["payment_status"])
        return {"request_id": data["request_id"], "payment_status": data["payment_status"]}, [], (
            f"Run {run.id}: The lunch draft now shows {data['payment_status']}. "
            "The invite is ready; the venue still hasn't confirmed a reservation."
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
            f"Run {run.id}: Luma created event {event_id}. Invitations were requested for "
            f"{len(plan.guests)} guest(s); {len(skipped)} were skipped by Luma. "
            "Check the event guest list for final delivery status."
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
        return {"event_id": event_id, "ticket_id": ticket_id, "url": url}, [], (
            f"Run {run.id}: Eventbrite published a free RSVP page for {plan.capacity} people: {url}. "
            "The registration page is live; no individual invitation email was sent."
        ), False
    if job.role == "Treasurer" and job.kind == "pay":
        payment = pay_pending_order(data["order_id"], is_admin=_admin_user(run.source_user))
        result = {"request_id": data["request_id"], "order_id": data["order_id"],
                  "payment_id": payment.id, "payment_status": payment.status,
                  "blocked_rule": payment.blocked_rule}
        if payment.status in UNCERTAIN_PAYMENT_STATUSES:
            return result, [], f"Run {run.id}: payment outcome is uncertain. The run is held for reconciliation; no retry was made.", True
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
            message = (f"Run {run.id}: Budget cleared. I submitted the sandbox transfer for "
                       f"{_money(payment.amount_cents, payment.currency)}. "
                       "Airwallex has it; settlement is not confirmed.")
        elif payment.status == "SIMULATED":
            message = (f"Run {run.id}: Budget says yes. I recorded "
                       f"a {_money(payment.amount_cents, payment.currency)} demo checkout. "
                       "Order receipt is in the run record.")
        elif next_jobs and next_jobs[0][1] == "requote":
            message = f"Run {run.id}: policy blocked {payment.blocked_rule}; Buyer is preparing a corrected quote."
        else:
            message = f"Run {run.id}: payment {payment.status}" + (
                f" ({payment.blocked_rule})." if payment.blocked_rule else ".")
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
        ts = post_role_update(role, channel_id, message, thread_ts=thread_ts)
        if role == "Concierge" and not thread_ts:
            with SessionLocal.begin() as session:
                session.add(ControlFlag(key=key, value=ts))
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
        _notify(run_id, role, f"Run {run_id}: {role} stopped; operator review is required before another attempt.")
    return True
