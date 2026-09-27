from __future__ import annotations

import asyncio
import hmac
import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .audit import record
from .config import settings
from .db import AgentJob, AuditEvent, Budget, ControlFlag, CrewRun, Payment, PendingOrder, Request, SessionLocal, Task, Vendor
from .inference import VultrInference
from .seed import seed_demo


app = FastAPI(title="Pengwin Crew", docs_url=None, redoc_url=None)
WEB = Path(__file__).resolve().parents[1] / "web"
app.mount("/static", StaticFiles(directory=WEB), name="static")


@app.middleware("http")
async def security_headers(request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'"
    return response


@app.on_event("startup")
def startup() -> None:
    seed_demo()


def require_admin(authorization: str | None = Header(default=None)) -> None:
    if len(settings.admin_token) < 24:
        raise HTTPException(503, "ADMIN_TOKEN must be configured with at least 24 characters")
    expected = f"Bearer {settings.admin_token}"
    if authorization is None or not hmac.compare_digest(authorization, expected):
        raise HTTPException(401, "Admin token required", headers={"WWW-Authenticate": "Bearer"})


def require_demo(authorization: str | None = Header(default=None)) -> str:
    if len(settings.web_demo_token) < 24:
        raise HTTPException(503, "WEB_DEMO_TOKEN must be configured")
    if authorization is None:
        raise HTTPException(401, "Demo token required", headers={"WWW-Authenticate": "Bearer"})
    if len(settings.admin_token) >= 24 and hmac.compare_digest(authorization, f"Bearer {settings.admin_token}"):
        return "admin"
    if hmac.compare_digest(authorization, f"Bearer {settings.web_demo_token}"):
        return "demo"
    raise HTTPException(401, "Demo token required", headers={"WWW-Authenticate": "Bearer"})


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "planner_mode": settings.planner_mode,
            "model": settings.vultr_model if settings.planner_mode == "vultr" else None,
            "sandbox_mode": settings.sandbox_mode, "payment_mode": settings.payment_mode}


@app.get("/api/state", dependencies=[Depends(require_admin)])
def state():
    with SessionLocal() as session:
        budgets = session.execute(select(Budget).order_by(Budget.office_id, Budget.category)).scalars().all()
        events = session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(100)).scalars().all()
        payments = session.execute(select(Payment).order_by(Payment.created_at.desc()).limit(30)).scalars().all()
        freeze = session.get(ControlFlag, "freeze")
        return {
            "company": settings.company_name,
            "modes": {"planner": settings.planner_mode, "sandbox": settings.sandbox_mode,
                      "payment": settings.payment_mode,
                      "model": settings.vultr_model if settings.planner_mode == "vultr" else None},
            "freeze": bool(freeze and freeze.value == "true") or settings.freeze,
            "budgets": [{"office": b.office_id, "category": b.category, "limit_cents": b.limit_cents,
                         "spent_cents": b.spent_cents, "reserved_cents": b.reserved_cents,
                         "currency": "USD" if b.office_id == "SF" else "EUR"} for b in budgets],
            "events": [{"id": e.id, "ts": e.ts.isoformat(), "request_id": e.request_id,
                        "agent": e.agent, "action": e.action, "severity": e.severity,
                        "detail": json.loads(e.detail_json)} for e in events],
            "payments": [{"id": p.id, "order_id": p.pending_order_id, "amount_cents": p.amount_cents,
                          "currency": p.currency, "status": p.status, "blocked_rule": p.blocked_rule,
                          "provider_ref": p.provider_ref, "simulated": p.simulated} for p in payments],
            "permissions": [
                {"agent": "Concierge", "slack": True, "quote": False, "browse": False, "pay": False},
                {"agent": "Buyer", "slack": False, "quote": True, "browse": True, "pay": False},
                {"agent": "Events", "slack": False, "quote": False, "browse": True, "pay": False},
                {"agent": "Treasurer", "slack": False, "quote": False, "browse": False, "pay": "policy only"},
            ],
        }


@app.get("/api/events", dependencies=[Depends(require_admin)])
async def events():
    async def stream():
        last_id = 0
        while True:
            with SessionLocal() as session:
                new_events = session.execute(select(AuditEvent).where(AuditEvent.id > last_id).order_by(AuditEvent.id).limit(100)).scalars().all()
                for event in new_events:
                    last_id = event.id
                    yield f"id: {event.id}\nevent: audit\ndata: {json.dumps({'id': event.id, 'agent': event.agent, 'action': event.action, 'severity': event.severity})}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(2)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.get("/api/receipts/{payment_id}", dependencies=[Depends(require_admin)])
def receipt(payment_id: str):
    with SessionLocal() as session:
        payment = session.get(Payment, payment_id)
        if payment is None:
            raise HTTPException(404)
        order = session.get(PendingOrder, payment.pending_order_id)
        request = session.get(Request, payment.request_id)
        vendor = session.get(Vendor, order.vendor_id)
        return {"payment_id": payment.id, "status": payment.status,
                "blocked_rule": payment.blocked_rule, "provider_ref": payment.provider_ref,
                "provider_error": payment.provider_error, "simulated": payment.simulated,
                "request": request.text, "office": request.office_id,
                "vendor": vendor.name, "sku": order.sku, "requested_qty": request.requested_qty,
                "ordered_qty": order.qty, "amount_cents": order.amount_cents,
                "currency": order.currency, "pending_order_id": order.id,
                "vendor_order_id": order.vendor_order_id}


@app.get("/api/invites/{request_id}", dependencies=[Depends(require_admin)])
def invite(request_id: str):
    with SessionLocal() as session:
        task = session.execute(select(Task).where(Task.request_id == request_id, Task.agent == "Events", Task.kind == "lunch")).scalar_one_or_none()
        if task is None:
            raise HTTPException(404)
        ics = json.loads(task.output_json)["ics"]
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": 'attachment; filename="berlin-team-lunch.ics"'})


@app.post("/api/demo/{flow}", dependencies=[Depends(require_admin)])
def demo(flow: str):
    from .jobs import submit_web_demo_run

    try:
        return {"run_id": submit_web_demo_run(flow)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class FreezeInput(BaseModel):
    frozen: bool


class CodeGoal(BaseModel):
    goal: str = Field(min_length=1, max_length=1000)


@app.get("/api/demo-access")
def demo_access(role: str = Depends(require_demo)):
    return {"access": role}


_JUDGE_ROLES = {"Concierge", "Buyer", "Events", "Treasurer", "Policy", "Control"}
_JUDGE_FLOWS = {
    "berlin-pantry": "Berlin pantry demo", "welcome-kit": "Welcome kit demo",
    "hoodie-attack": "Hoodie safety demo", "natural-language": "Crew request",
    "luma-event": "Luma event draft", "eventbrite-event": "Free RSVP event",
    "code-task": "Sandbox code task", "project-plan": "Event and swag plan",
    "event-status": "Event status check",
}
_JUDGE_KINDS = {
    ("Concierge", "dispatch"): "Coordinate",
    ("Buyer", "purchase"): "Check mock offer", ("Buyer", "requote"): "Check corrected offer",
    ("Buyer", "code_execute"): "Execute code", ("Buyer", "product_source"): "Source water bottles",
    ("Events", "lunch_prepare"): "Prepare lunch", ("Events", "event_research"): "Research event options",
    ("Events", "lunch_complete"): "Finish lunch plan", ("Events", "luma_publish"): "Publish Luma event",
    ("Events", "eventbrite_publish"): "Publish RSVP page", ("Events", "eventbrite_status"): "Check RSVP status",
    ("Treasurer", "pay"): "Check payment", ("Treasurer", "budget_review"): "Review estimated budget",
}
_JUDGE_STATUSES = {"QUEUED", "RUNNING", "WAITING_APPROVAL", "COMPLETE", "DONE", "FAILED", "HELD", "REJECTED"}


def _utc_iso(value: datetime) -> str:
    # SQLite drops the timezone even though these values are written in UTC.
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).isoformat()
_JUDGE_ACTIONS = {
    "run_queued": ("Request queued", "queued", "Control plane accepted the request."),
    "web_code_queued": ("Code task queued", "queued", "The sandbox has not reported an output yet."),
    "web_demo_queued": ("Demo workflow queued", "queued", "Control plane accepted the demo request."),
    "request_planned": ("Workflow prepared", "complete", "A plan was recorded for the crew."),
    "selection_proposed": ("Mock offer selected", "complete", "Buyer recorded a proposed choice."),
    "selection_rejected": ("Mock offer rejected", "blocked", "Buyer rejected a mismatched choice."),
    "requote_after_block": ("Mock offer corrected", "complete", "Buyer checked the quantity again."),
    "pending_order_created": ("Proposed order recorded", "proposed", "The amount is a mock-store quote, not a charge."),
    "event_task_created": ("Event task prepared", "complete", "Events received a plan to review."),
    "lunch_planned": ("Lunch plan prepared", "complete", "No venue reservation is implied."),
    "lunch_payment_status": ("Lunch payment status recorded", "complete", "See the separate spending labels below."),
    "event_approved": ("Event draft approved", "approved", "A human approval was recorded before publication."),
    "event_rejected": ("Event draft rejected", "blocked", "Publication was stopped."),
    "eventbrite_draft_created": ("Eventbrite draft created", "complete", "The provider returned a draft record."),
    "eventbrite_free_ticket_created": ("Free RSVP ticket created", "complete", "The provider returned a ticket record."),
    "luma_event_created": ("Luma event created", "complete", "The provider returned an event record."),
    "payment_blocked": ("Payment blocked", "blocked", "The policy gate stopped the payment."),
    "payment_reserved": ("Budget reserved", "reserved", "Reservation is an internal ledger state."),
    "provider_error": ("Payment outcome needs review", "held", "No settlement is confirmed by this record."),
    "agent_job_held": ("Agent step held", "held", "The worker stopped for review."),
    "agent_job_failed": ("Agent step failed", "failed", "The worker stopped without claiming success."),
    "slack_update_failed": ("Slack update failed", "failed", "The durable task record remains available."),
}


def _judge_event(event: AuditEvent) -> dict | None:
    """Build a fixed, low-detail proof item; never return audit detail verbatim."""
    if event.agent not in _JUDGE_ROLES:
        return None
    template = _JUDGE_ACTIONS.get(event.action)
    detail = {}
    if event.action in {"sandbox_code_attempt", "payment_submitted", "agent_job_done", "freeze_changed"}:
        try:
            detail = json.loads(event.detail_json)
            if not isinstance(detail, dict):
                detail = {}
        except (TypeError, ValueError):
            detail = {}
    if event.action == "sandbox_code_attempt":
        attempt = detail.get("attempt")
        exit_code = detail.get("exit_code")
        code_hash = detail.get("code_hash")
        if (type(attempt) is not int or not 1 <= attempt <= 2
                or type(exit_code) is not int or not -255 <= exit_code <= 255
                or not isinstance(code_hash, str) or not re.fullmatch(r"[0-9a-f]{12}", code_hash)):
            return None
        status = "contained" if exit_code == 124 else "complete" if exit_code == 0 else "failed"
        title = "Code attempt contained" if exit_code == 124 else "Code attempt executed"
        proof = f"Attempt {attempt} · exit {exit_code} · SHA-256 prefix {code_hash}"
    elif event.action == "payment_submitted":
        if detail.get("simulated") is True:
            title, status, proof = "Demo checkout recorded", "simulated", "Local simulation; no real charge."
        elif detail.get("simulated") is False:
            provider_status = detail.get("provider_status")
            if not isinstance(provider_status, str):
                return None
            if provider_status.upper() in {"FAILED", "CANCELLED", "APPROVAL_REJECTED"}:
                title, status, proof = "Sandbox transfer rejected", "blocked", "Provider reported rejection."
            else:
                title, status, proof = "Sandbox transfer submitted", "submitted", "Provider submission; settlement unconfirmed."
        else:
            return None
    elif event.action == "agent_job_done":
        task = _JUDGE_KINDS.get((event.agent, detail.get("kind")))
        title, status, proof = (f"{task} completed" if task else "Agent step completed"), "complete", "A durable worker result was recorded."
    elif event.action == "freeze_changed":
        frozen = detail.get("frozen")
        if type(frozen) is not bool:
            return None
        title, status, proof = ("Payment gate frozen", "held", "New payments are blocked.") if frozen else (
            "Payment gate unfrozen", "complete", "The admin restored the payment gate.")
    elif template is not None:
        title, status, proof = template
    else:
        return None
    return {"ts": _utc_iso(event.ts), "role": event.agent, "title": title,
            "status": status, "evidence": proof}


def _currency_totals(session: Session, model, allowed_status: str | None = None,
                     simulated: bool | None = None) -> list[dict]:
    query = (select(model.currency, func.sum(model.amount_cents), func.count(model.id))
             .where(model.currency.in_(("USD", "EUR")), model.amount_cents >= 0)
             .group_by(model.currency).order_by(model.currency))
    if allowed_status is not None:
        query = query.where(model.status == allowed_status)
    if simulated is not None:
        query = query.where(model.simulated.is_(simulated))
    return [{"currency": currency, "amount_cents": int(total), "count": count}
            for currency, total, count in session.execute(query)]


def _judge_run(run: CrewRun, jobs: list[AgentJob]) -> dict | None:
    flow = _JUDGE_FLOWS.get(run.flow)
    if flow is None or run.status not in _JUDGE_STATUSES:
        return None
    steps = [{"role": job.role, "task": _JUDGE_KINDS[(job.role, job.kind)], "status": job.status}
             for job in jobs if (job.role, job.kind) in _JUDGE_KINDS and job.status in _JUDGE_STATUSES]
    return {"flow": flow, "status": run.status, "created_at": _utc_iso(run.created_at), "steps": steps[:8]}


def _price_cents(value: object, *, ceiling: int) -> int | None:
    if type(value) not in {int, float}:
        return None
    try:
        cents = Decimal(str(value)) * 100
    except InvalidOperation:
        return None
    if not cents.is_finite() or cents != cents.to_integral_value() or not 0 <= cents <= ceiling:
        return None
    return int(cents)


def _judge_product_estimate(job: AgentJob) -> dict | None:
    """Expose only a checked catalog range from a completed sourcing step."""
    if (job.role, job.kind, job.status) != ("Buyer", "product_source", "DONE"):
        return None
    try:
        output = json.loads(job.output_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(output, dict) or any((
        output.get("status") != "RESEARCHED",
        output.get("product") != "water_bottle",
        output.get("publisher_status") != "ok",
        output.get("price_kind") != "PRODUCT_RANGE_ESTIMATE",
        output.get("checkout_status") != "NOT_READY",
        output.get("currency") != "USD",
        output.get("source_url") != "https://www.printful.com/custom-water-bottles",
    )):
        return None
    try:
        checked_at = datetime.fromisoformat(output["checked_at"].replace("Z", "+00:00"))
    except (KeyError, AttributeError, TypeError, ValueError):
        return None
    if checked_at.tzinfo is None or checked_at > datetime.now(timezone.utc) + timedelta(minutes=5):
        return None
    low = _price_cents(output.get("unit_min"), ceiling=100_000)
    high = _price_cents(output.get("unit_max"), ceiling=100_000)
    if low is None or high is None or low <= 0 or high < low:
        return None
    quantity = output.get("quantity")
    subtotal_low = subtotal_high = None
    if quantity is not None:
        if type(quantity) is not int or not 1 <= quantity <= 1000:
            return None
        subtotal_low = _price_cents(output.get("subtotal_min"), ceiling=100_000_000)
        subtotal_high = _price_cents(output.get("subtotal_max"), ceiling=100_000_000)
        if subtotal_low != low * quantity or subtotal_high != high * quantity:
            return None
    elif output.get("subtotal_min") is not None or output.get("subtotal_max") is not None:
        return None
    return {"currency": "USD", "unit_min_cents": low, "unit_max_cents": high,
            "quantity": quantity, "subtotal_min_cents": subtotal_low,
            "subtotal_max_cents": subtotal_high, "checked_at": _utc_iso(checked_at),
            "source_url": "https://www.printful.com/custom-water-bottles"}


def _judge_activity_payload():
    """A fixed public projection with no request text, identifiers, or credentials."""
    with SessionLocal() as session:
        rows = session.execute(select(AuditEvent).order_by(AuditEvent.id.desc()).limit(160)).scalars().all()
        activity = [item for row in rows if (item := _judge_event(row)) is not None][:36]
        recent_runs = session.execute(select(CrewRun).order_by(CrewRun.created_at.desc()).limit(8)).scalars().all()
        recent_jobs = session.execute(select(AgentJob).where(AgentJob.run_id.in_([run.id for run in recent_runs]))
                                      .order_by(AgentJob.created_at)).scalars().all() if recent_runs else []
        jobs_by_run: dict[str, list[AgentJob]] = {run.id: [] for run in recent_runs}
        for job in recent_jobs:
            jobs_by_run[job.run_id].append(job)
        runs = [item for run in recent_runs if (item := _judge_run(run, jobs_by_run[run.id])) is not None]
        proposed = _currency_totals(session, PendingOrder)
        simulated = _currency_totals(session, Payment, "SIMULATED", True)
        submitted_sandbox = _currency_totals(session, Payment, "SUBMITTED_SANDBOX", False)
        product_sources = session.execute(select(AgentJob).where(
            AgentJob.role == "Buyer", AgentJob.kind == "product_source", AgentJob.status == "DONE",
        ).order_by(AgentJob.updated_at.desc(), AgentJob.id.desc()).limit(20)).scalars().all()
        product_estimate = next((item for job in product_sources
                                 if (item := _judge_product_estimate(job)) is not None), None)
        counter = session.get(ControlFlag, "vultr_calls")
    try:
        attempted_calls = max(0, int(counter.value)) if counter else 0
    except ValueError:
        attempted_calls = 0
    return {
        "as_of": datetime.now(timezone.utc).isoformat(),
        "activity": activity,
        "runs": runs,
        "spend": {
            "proposed_mock_orders": proposed,
            "water_bottle_product_estimate": product_estimate,
            "simulated_checkouts": simulated,
            "submitted_sandbox_transfers": submitted_sandbox,
            "real_settled": None,
        },
        "model_usage": {"model": settings.vultr_model if settings.planner_mode == "vultr" else None,
                        "attempted_calls": attempted_calls, "call_limit": settings.vultr_max_calls,
                        "billed_cost_usd": None},
    }


@app.get("/api/public-activity")
def public_activity():
    """Give judges a read-only activity view without sharing a demo token."""
    if not settings.public_judge_feed:
        raise HTTPException(404)
    return _judge_activity_payload()


@app.get("/api/judge-activity")
def judge_activity(_role: str = Depends(require_demo)):
    """Keep the original authenticated read route for demo clients."""
    return _judge_activity_payload()


@app.post("/api/code-runs", dependencies=[Depends(require_demo)])
def queue_code_run(body: CodeGoal):
    from .jobs import submit_web_code_run

    try:
        return {"run_id": submit_web_code_run(body.goal)}
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/code-runs/{run_id}", dependencies=[Depends(require_demo)])
def code_run(run_id: str):
    from .jobs import get_run

    run = get_run(run_id)
    if run is None or run["flow"] != "code-task" or run["source_user"] != "web-demo":
        raise HTTPException(404)
    return run


@app.post("/api/freeze", dependencies=[Depends(require_admin)])
def freeze(body: FreezeInput):
    with SessionLocal.begin() as session:
        flag = session.get(ControlFlag, "freeze")
        flag.value = "true" if body.frozen else "false"
        record(session, agent="Control", action="freeze_changed", detail={"frozen": body.frozen},
               severity="warning" if body.frozen else "info")
    return {"frozen": body.frozen}


@app.post("/api/reset", dependencies=[Depends(require_admin)])
def reset_demo():
    if settings.payment_mode != "simulated":
        raise HTTPException(409, "Reset is available only in simulated payment mode")
    seed_demo(reset=True)
    return {"reset": True}


@app.get("/api/vultr/models", dependencies=[Depends(require_admin)])
def vultr_models():
    try:
        return {"models": VultrInference().list_models()}
    except Exception as exc:
        raise HTTPException(502, f"Vultr model check failed: {type(exc).__name__}") from exc
