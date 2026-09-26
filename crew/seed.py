from __future__ import annotations

import os

from sqlalchemy import delete, select

from .db import AuditEvent, Budget, ControlFlag, Office, Payment, PendingOrder, Request, Task, Vendor, SessionLocal, init_db


def seed_demo(*, reset: bool = False) -> None:
    init_db()
    with SessionLocal.begin() as session:
        if reset:
            for model in (Payment, PendingOrder, Task, Request, AuditEvent, Budget, Vendor, Office):
                session.execute(delete(model))
            # Demo resets must not refresh the paid-inference allowance.
            session.execute(delete(ControlFlag).where(ControlFlag.key != "vultr_calls"))
        if session.get(Office, "SF") is None:
            session.add_all([
                Office(id="SF", name="San Francisco", currency="USD"),
                Office(id="BER", name="Berlin", currency="EUR"),
            ])
        if session.execute(select(Budget.id).limit(1)).first() is None:
            session.add_all([
                Budget(office_id="SF", category="office-supplies", limit_cents=150000, spent_cents=0, reserved_cents=0),
                Budget(office_id="SF", category="swag", limit_cents=250000, spent_cents=0, reserved_cents=0),
                Budget(office_id="BER", category="office-supplies", limit_cents=120000, spent_cents=0, reserved_cents=0),
                Budget(office_id="BER", category="swag", limit_cents=180000, spent_cents=0, reserved_cents=0),
                Budget(office_id="BER", category="onboarding", limit_cents=100000, spent_cents=0, reserved_cents=0),
                Budget(office_id="BER", category="events", limit_cents=100000, spent_cents=0, reserved_cents=0),
            ])
        for vendor in (
            Vendor(id="kaffee-kontor", office_id="BER", name="Kaffee Kontor", currency="EUR", kind="mock", beneficiary_id=os.getenv("AW_BENEFICIARY_KAFFEE") or None),
            Vendor(id="druckwerk", office_id="BER", name="Druckwerk", currency="EUR", kind="mock", beneficiary_id=os.getenv("AW_BENEFICIARY_DRUCKWERK") or None),
            Vendor(id="bay-supply", office_id="SF", name="Bay Supply", currency="USD", kind="mock", beneficiary_id=os.getenv("AW_BENEFICIARY_BAY_SUPPLY") or None),
        ):
            if session.get(Vendor, vendor.id) is None:
                session.add(vendor)
        if session.get(ControlFlag, "freeze") is None:
            session.add(ControlFlag(key="freeze", value="false"))
        if session.get(ControlFlag, "vultr_calls") is None:
            session.add(ControlFlag(key="vultr_calls", value="0"))
