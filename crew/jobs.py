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
from .db import AgentJob, ControlFlag, CrewRun, PendingOrder, Request, SessionLocal
from .payments import pay_pending_order
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
}


def flow_label(flow: str) -> str:
    return FLOW_LABELS.get(flow, flow.replace("-", " "))

ROLE_KINDS = {
    "Concierge": {"dispatch"},
    "Buyer": {"purchase", "requote"},
    "Events": {"lunch_prepare", "lunch_complete"},
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


def submit_run(flow: str, *, source_user: str, channel_id: str) -> str:
    if flow not in FLOW_STEPS:
        raise ValueError("Unknown crew flow")
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
        _new_job(session, run_id=run_id, role="Concierge", kind="dispatch", input_data={"flow": flow})
        record(session, agent="Concierge", action="run_queued", request_id=run_id,
               detail={"flow": flow, "source_user": source_user})
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


def _perform(job: AgentJob, run: CrewRun) -> tuple[dict, list[tuple[str, str, dict]], str, bool]:
    if job.kind not in ROLE_KINDS.get(job.role, set()):
        raise ValueError("Job kind exceeds role capability")
    data = json.loads(job.input_json)
    if job.role == "Concierge":
        steps = FLOW_STEPS[run.flow]
        return {"steps": len(steps)}, list(steps), f"Run {run.id}: accepted {flow_label(run.flow)} and assigned {len(steps)} tasks.", False
    if job.role == "Buyer" and job.kind == "purchase":
        prepared = prepare_purchase(**data, source="slack", source_user=run.source_user)
        message = (f"Run {run.id}: inspected the fictional vendor and prepared "
                   f"{prepared['proposed_qty']} × {prepared['proposed_sku']} for policy review.")
        next_jobs = [("Treasurer", "pay", {"order_id": prepared["first_order_id"],
                                            "request_id": prepared["request_id"]})]
        return prepared, next_jobs, message, False
    if job.role == "Buyer" and job.kind == "requote":
        order_id = requote_purchase(data["request_id"])
        result = {"request_id": data["request_id"], "order_id": order_id}
        next_jobs = [("Treasurer", "pay", {"order_id": order_id, "request_id": data["request_id"]})]
        return result, next_jobs, f"Run {run.id}: replaced the blocked quote with the requested quantity.", False
    if job.role == "Events" and job.kind == "lunch_prepare":
        prepared = prepare_berlin_lunch(**data, source="slack", source_user=run.source_user)
        next_jobs = [("Treasurer", "pay", {"order_id": prepared["first_order_id"],
                                            "request_id": prepared["request_id"], "lunch": True})]
        return prepared, next_jobs, f"Run {run.id}: drafted a fictional Berlin lunch and calendar invite; payment remains pending.", False
    if job.role == "Events" and job.kind == "lunch_complete":
        complete_berlin_lunch(data["request_id"], data["payment_status"])
        return {"request_id": data["request_id"], "payment_status": data["payment_status"]}, [], (
            f"Run {run.id}: lunch draft updated to payment status {data['payment_status']}; this is not a confirmed reservation."
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
            message = f"Run {run.id}: sandbox transfer submitted; settlement is not confirmed."
        elif payment.status == "SIMULATED":
            message = f"Run {run.id}: policy approved and local payment was simulated."
        elif next_jobs and next_jobs[0][1] == "requote":
            message = f"Run {run.id}: policy blocked {payment.blocked_rule}; Buyer is preparing a corrected quote."
        else:
            message = f"Run {run.id}: payment {payment.status}" + (
                f" ({payment.blocked_rule})." if payment.blocked_rule else ".")
        return result, next_jobs, message, False
    raise ValueError("Unsupported job")


def _notify(run_id: str, role: str, message: str) -> None:
    key = "crew_thread_" + hashlib.sha256(run_id.encode()).hexdigest()[:24]
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
                    _new_job(session, run_id=run_id, role=next_role, kind=kind,
                             input_data=input_data)
                _refresh_run(session, run_id)
            record(session, agent=role, action="agent_job_held" if hold else "agent_job_done",
                   request_id=run_id, detail={"job_id": job_id, "kind": job.kind,
                                              "next_jobs": len(successors)}, severity="warning" if hold else "info")
        _notify(run_id, role, message)
    except Exception as exc:
        with SessionLocal.begin() as session:
            persisted = session.get(AgentJob, job_id)
            if persisted and persisted.status == "RUNNING":
                persisted.status = "HELD" if role == "Treasurer" else "FAILED"
                persisted.error = type(exc).__name__
                persisted.updated_at = datetime.now(timezone.utc)
                _stop_run(session, run_id, status="HELD" if role == "Treasurer" else "FAILED")
                record(session, agent=role, action="agent_job_failed", request_id=run_id,
                       detail={"job_id": job_id, "kind": job.kind,
                               "error_type": type(exc).__name__}, severity="error")
        _notify(run_id, role, f"Run {run_id}: {role} stopped; operator review is required before another attempt.")
    return True
