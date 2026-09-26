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


def run_purchase(*, office: str, sku: str, qty: int, source: str = "web", source_user: str = "demo",
                 text: str | None = None, is_admin: bool = False) -> dict:
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
    if choice.sku != sku or choice.quantity < 1 or choice.quantity > 1000:
        raise ValueError("Buyer returned an invalid SKU or quantity")
    with SessionLocal.begin() as session:
        _task(session, request_id, agent="Buyer", kind="select",
              input_data={"sku": sku, "qty": qty, "page_text": page_text[:2000]},
              output_data=choice.model_dump())
        record(session, agent="Buyer", action="selection_proposed", request_id=request_id,
               detail={"sku": choice.sku, "qty": choice.quantity,
                       "planner_mode": settings.planner_mode})

    first_order = _pending_order(request_id=request_id, sku=sku, qty=choice.quantity, browser=browser)
    first_payment = pay_pending_order(first_order.id, is_admin=is_admin)
    result = {"request_id": request_id, "first_order_id": first_order.id,
              "first_payment_id": first_payment.id, "first_status": first_payment.status,
              "blocked_rule": first_payment.blocked_rule, "planner_mode": settings.planner_mode,
              "sandbox_mode": settings.sandbox_mode, "payment_mode": settings.payment_mode}
    if first_payment.blocked_rule == "QUANTITY_SANITY" and choice.quantity != qty:
        corrected = _pending_order(request_id=request_id, sku=sku, qty=qty, browser=browser)
        corrected_payment = pay_pending_order(corrected.id, is_admin=is_admin)
        with SessionLocal.begin() as session:
            record(session, agent="Buyer", action="requote_after_block", request_id=request_id,
                   detail={"original_qty": choice.quantity, "corrected_qty": qty,
                           "new_order_id": corrected.id})
        result.update({"corrected_order_id": corrected.id,
                       "corrected_payment_id": corrected_payment.id,
                       "corrected_status": corrected_payment.status,
                       "corrected_blocked_rule": corrected_payment.blocked_rule})
    return result


def run_berlin_lunch(*, headcount: int = 8, source: str = "web", source_user: str = "demo",
                      is_admin: bool = False) -> dict:
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
    payment = pay_pending_order(order.id, is_admin=is_admin)
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
                           "payment_status": payment.status, "ics": invite})
        record(session, agent="Events", action="lunch_planned", request_id=request_id,
               detail={"venue": "Kaffee Kontor", "headcount": headcount,
                       "date": lunch_day.isoformat(), "payment_status": payment.status})
    return {"request_id": request_id, "first_order_id": order.id, "first_payment_id": payment.id,
            "first_status": payment.status, "blocked_rule": payment.blocked_rule,
            "planner_mode": "deterministic-events", "sandbox_mode": settings.sandbox_mode,
            "payment_mode": settings.payment_mode, "invite_ready": True}


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
