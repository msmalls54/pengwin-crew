from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select

from .audit import record
from .catalog import PRODUCTS
from .config import settings
from .db import PendingOrder, Request, SessionLocal, Task
from .inference import BuyerChoice, VultrInference
from .payments import pay_pending_order
from .sandbox import sandbox


def _task(session, request_id: str, *, agent: str, kind: str, input_data: dict, output_data: dict, status: str = "DONE") -> None:
    session.add(Task(id=str(uuid4()), request_id=request_id, agent=agent, kind=kind, status=status,
                     input_json=json.dumps(input_data), output_json=json.dumps(output_data)))


def _pending_order(*, request_id: str, sku: str, qty: int, browser) -> PendingOrder:
    quoted = browser.pending_order(sku, qty)
    expected = PRODUCTS[sku]
    if (quoted.get("sku") != sku or quoted.get("qty") != qty or
        quoted.get("vendor_id") != expected.vendor_id or
        quoted.get("currency") != expected.currency or
        quoted.get("amount_cents") != expected.price_cents * qty):
        raise ValueError("Sandbox quote differs from canonical mock catalog")
    order = PendingOrder(id=str(uuid4()), request_id=request_id, vendor_id=expected.vendor_id,
                         sku=sku, qty=qty, amount_cents=quoted["amount_cents"],
                         currency=quoted["currency"], vendor_order_id=quoted["vendor_order_id"])
    with SessionLocal.begin() as session:
        session.add(order)
        record(session, agent="Buyer", action="pending_order_created", request_id=request_id,
               detail={"order_id": order.id, "vendor_order_id": order.vendor_order_id,
                       "sku": sku, "qty": qty, "amount_cents": order.amount_cents})
    return order


def prepare_purchase(*, office: str, sku: str, qty: int, source: str = "web", source_user: str = "demo",
                     text: str | None = None) -> dict:
    if sku not in PRODUCTS or qty < 1 or qty > 1000:
        raise ValueError("Unknown SKU or invalid quantity")
    product = PRODUCTS[sku]
    if (office == "BER" and product.currency != "EUR") or (office == "SF" and product.currency != "USD"):
        raise ValueError("Product does not belong to this office")
    browser = sandbox()
    request_id = str(uuid4())
    with SessionLocal.begin() as session:
        request = Request(id=request_id, source=source, source_user=source_user,
                          text=text or f"Buy {qty} {product.name}", office_id=office,
                          category=product.category, sku=sku, requested_qty=qty, status="PLANNED")
        session.add(request)
        _task(session, request_id, agent="Concierge", kind="plan",
              input_data={"text": request.text}, output_data={"office": office, "sku": sku, "qty": qty})
        record(session, agent="Concierge", action="request_planned", request_id=request_id,
               detail={"office": office, "sku": sku, "qty": qty})

    page_text = browser.inspect(sku)
    if settings.planner_mode == "vultr":
        choice = VultrInference().buyer_choice(requested_sku=sku, requested_qty=qty, page_text=page_text)
    elif settings.planner_mode == "deterministic":
        # Replays a compromised buyer decision for the attack demo. It is not an LLM result.
        attack_qty = 500 if sku == "HOODIE-BER" else qty
        choice = BuyerChoice(sku=sku, quantity=attack_qty,
                             reason="Local attack simulation" if attack_qty != qty else "Requested quantity")
    else:
        raise RuntimeError("Unknown planner mode")
    if choice.sku not in PRODUCTS:
        with SessionLocal.begin() as session:
            record(session, agent="Buyer", action="selection_rejected", request_id=request_id,
                   detail={"reason": "UNKNOWN_SKU", "proposed_sku": choice.sku}, severity="error")
        raise ValueError("Buyer returned an unknown SKU")
    with SessionLocal.begin() as session:
        _task(session, request_id, agent="Buyer", kind="select",
              input_data={"sku": sku, "qty": qty, "page_text": page_text[:2000]},
              output_data=choice.model_dump())
        record(session, agent="Buyer", action="selection_proposed", request_id=request_id,
               detail={"sku": choice.sku, "qty": choice.quantity,
                       "planner_mode": settings.planner_mode,
                       "model": settings.vultr_model if settings.planner_mode == "vultr" else None})

    first_order = _pending_order(request_id=request_id, sku=choice.sku, qty=choice.quantity, browser=browser)
    return {"request_id": request_id, "first_order_id": first_order.id,
            "requested_sku": sku, "requested_qty": qty,
            "proposed_sku": choice.sku, "proposed_qty": choice.quantity,
            "product_name": PRODUCTS[choice.sku].name,
            "amount_cents": first_order.amount_cents, "currency": first_order.currency,
            "planner_mode": settings.planner_mode,
            "model": settings.vultr_model if settings.planner_mode == "vultr" else None,
            "sandbox_mode": settings.sandbox_mode, "payment_mode": settings.payment_mode}


def requote_purchase(request_id: str) -> str:
    with SessionLocal() as session:
        request = session.get(Request, request_id)
        if request is None or request.sku not in PRODUCTS:
            raise ValueError("Purchase request is missing")
        sku, qty = request.sku, request.requested_qty
    corrected = _pending_order(request_id=request_id, sku=sku, qty=qty, browser=sandbox())
    with SessionLocal.begin() as session:
        record(session, agent="Buyer", action="requote_after_block", request_id=request_id,
               detail={"corrected_sku": sku, "corrected_qty": qty, "new_order_id": corrected.id})
    return corrected.id


def run_purchase(*, office: str, sku: str, qty: int, source: str = "web", source_user: str = "demo",
                 text: str | None = None, is_admin: bool = False) -> dict:
    prepared = prepare_purchase(office=office, sku=sku, qty=qty, source=source,
                                source_user=source_user, text=text)
    request_id = prepared["request_id"]
    first_order_id = prepared["first_order_id"]
    first_payment = pay_pending_order(first_order_id, is_admin=is_admin)
    result = {"request_id": request_id, "first_order_id": first_order_id,
              "first_payment_id": first_payment.id, "first_status": first_payment.status,
              "blocked_rule": first_payment.blocked_rule, "planner_mode": prepared["planner_mode"],
              "model": prepared["model"], "sandbox_mode": prepared["sandbox_mode"],
              "payment_mode": prepared["payment_mode"]}
    if first_payment.blocked_rule in {"QUANTITY_SANITY", "SKU_MISMATCH", "VENDOR_ALLOWLIST"} and (
        prepared["proposed_sku"] != sku or prepared["proposed_qty"] != qty
    ):
        corrected_order_id = requote_purchase(request_id)
        corrected_payment = pay_pending_order(corrected_order_id, is_admin=is_admin)
        result.update({"corrected_order_id": corrected_order_id,
                       "corrected_payment_id": corrected_payment.id,
                       "corrected_status": corrected_payment.status,
                       "corrected_blocked_rule": corrected_payment.blocked_rule})
    return result


def prepare_berlin_lunch(*, headcount: int = 8, source: str = "web", source_user: str = "demo") -> dict:
    if not 1 <= headcount <= 40:
        raise ValueError("Invalid lunch headcount")
    berlin = ZoneInfo("Europe/Berlin")
    today = datetime.now(berlin).date()
    days = (0 - today.weekday()) % 7 or 7
    lunch_day = today + timedelta(days=days)
    start = datetime.combine(lunch_day, time(12, 0), tzinfo=berlin)
    end = start + timedelta(minutes=90)
    request_id = str(uuid4())
    with SessionLocal.begin() as session:
        session.add(Request(id=request_id, source=source, source_user=source_user,
                            text="First-day Berlin team lunch", office_id="BER", category="events",
                            sku="LUNCH-BER", requested_qty=headcount, status="PLANNED"))
        _task(session, request_id, agent="Concierge", kind="event-plan",
              input_data={"headcount": headcount}, output_data={"venue": "Kaffee Kontor", "date": lunch_day.isoformat()})
        record(session, agent="Concierge", action="event_task_created", request_id=request_id,
               detail={"headcount": headcount, "date": lunch_day.isoformat()})
    browser = sandbox()
    order = _pending_order(request_id=request_id, sku="LUNCH-BER", qty=headcount, browser=browser)
    invite = (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//Office Ops Crew//EN\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{request_id}@office-ops.invalid\r\n"
        f"DTSTAMP:{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}\r\n"
        f"DTSTART:{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\r\n"
        f"DTEND:{end.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}\r\n"
        "SUMMARY:Berlin new-hire team lunch\r\nLOCATION:Kaffee Kontor (fictional)\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    with SessionLocal.begin() as session:
        _task(session, request_id, agent="Events", kind="lunch",
              input_data={"headcount": headcount, "date": lunch_day.isoformat()},
              output_data={"venue": "Kaffee Kontor", "amount_cents": order.amount_cents,
                           "payment_status": "PENDING", "ics": invite})
        record(session, agent="Events", action="lunch_planned", request_id=request_id,
               detail={"venue": "Kaffee Kontor", "headcount": headcount,
                       "date": lunch_day.isoformat(), "payment_status": "PENDING"})
    return {"request_id": request_id, "first_order_id": order.id,
            "planner_mode": "deterministic-events", "sandbox_mode": settings.sandbox_mode,
            "payment_mode": settings.payment_mode, "invite_ready": True,
            "date": lunch_day.isoformat()}


def complete_berlin_lunch(request_id: str, payment_status: str) -> None:
    with SessionLocal.begin() as session:
        task = session.execute(select(Task).where(Task.request_id == request_id,
                                                  Task.agent == "Events", Task.kind == "lunch")).scalar_one()
        output = json.loads(task.output_json)
        output["payment_status"] = payment_status
        task.output_json = json.dumps(output)
        record(session, agent="Events", action="lunch_payment_status", request_id=request_id,
               detail={"payment_status": payment_status})


def run_berlin_lunch(*, headcount: int = 8, source: str = "web", source_user: str = "demo",
                      is_admin: bool = False) -> dict:
    prepared = prepare_berlin_lunch(headcount=headcount, source=source, source_user=source_user)
    payment = pay_pending_order(prepared["first_order_id"], is_admin=is_admin)
    complete_berlin_lunch(prepared["request_id"], payment.status)
    return {"request_id": prepared["request_id"], "first_order_id": prepared["first_order_id"], "first_payment_id": payment.id,
            "first_status": payment.status, "blocked_rule": payment.blocked_rule,
            "planner_mode": prepared["planner_mode"], "sandbox_mode": prepared["sandbox_mode"],
            "payment_mode": prepared["payment_mode"], "invite_ready": True}


def run_flow(flow: str, *, source: str = "web", source_user: str = "demo",
             is_admin: bool = False) -> list[dict]:
    if flow == "berlin-pantry":
        return [run_purchase(office="BER", sku="OAT-MILK", qty=6, text="Berlin is out of oat milk", source=source, source_user=source_user, is_admin=is_admin),
                run_purchase(office="BER", sku="COFFEE", qty=2, text="Berlin needs coffee", source=source, source_user=source_user, is_admin=is_admin)]
    if flow == "hoodie-attack":
        return [run_purchase(office="BER", sku="HOODIE-BER", qty=20,
                             text="Hoodies for the Berlin office", source=source, source_user=source_user, is_admin=is_admin),
                run_purchase(office="SF", sku="HOODIE-SF", qty=40,
                             text="Hoodies for the San Francisco office", source=source, source_user=source_user, is_admin=is_admin)]
    if flow == "welcome-kit":
        return [run_purchase(office="BER", sku="WELCOME-KIT", qty=1,
                             text="New hire starts in Berlin Monday", source=source, source_user=source_user, is_admin=is_admin),
                run_berlin_lunch(source=source, source_user=source_user, is_admin=is_admin)]
    raise ValueError("Unknown demo flow")
