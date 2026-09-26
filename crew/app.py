from __future__ import annotations

import asyncio
import hmac
import json
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, StreamingResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select

from .audit import record
from .config import settings
from .db import AuditEvent, Budget, ControlFlag, Payment, PendingOrder, Request, SessionLocal, Task, Vendor
from .inference import VultrInference
from .seed import seed_demo
from .workflow import run_flow


app = FastAPI(title="Office Ops Crew", docs_url=None, redoc_url=None)
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


@app.get("/")
def index():
    return FileResponse(WEB / "index.html")


@app.get("/health")
def health():
    return {"status": "ok", "planner_mode": settings.planner_mode,
            "model": settings.vultr_model if settings.planner_mode == "vultr" else None,
            "sandbox_mode": settings.sandbox_mode, "payment_mode": settings.payment_mode}


@app.get("/api/state")
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


@app.get("/api/events")
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


@app.get("/api/receipts/{payment_id}")
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


@app.get("/api/invites/{request_id}")
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
    try:
        return {"results": run_flow(flow)}
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


class FreezeInput(BaseModel):
    frozen: bool


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
