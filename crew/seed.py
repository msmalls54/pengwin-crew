from __future__ import annotations

import os

from sqlalchemy import delete, select

from .db import (AgentJob, AuditEvent, Budget, ControlFlag, ConversationTurn,
                 CrewProject, CrewRun, Office, Payment, PendingOrder,
                 ProjectRunLink, Request, RunResourceLink, Task, Vendor,
                 SessionLocal, init_db)
from .web_limits import WEB_RUN_COUNTERS, initialize_web_run_counters


def seed_demo(*, reset: bool = False) -> None:
    init_db()
    with SessionLocal.begin() as session:
        # On an upgrade, capture the legacy run total before reset deletes it.
        # Existing counters are never refreshed from a smaller visible total.
        initialize_web_run_counters(session)
        if reset:
            for model in (RunResourceLink, ProjectRunLink, ConversationTurn,
                          AgentJob, CrewProject, CrewRun, Payment, PendingOrder,
                          Task, Request, AuditEvent, Budget, Vendor, Office):
                session.execute(delete(model))
            # Demo resets must not refresh paid inference, search, web-run caps,
            # or Slack delivery claims.
            # Slack may redeliver an old event after a local reset.
            session.execute(delete(ControlFlag).where(
                ~ControlFlag.key.in_(("vultr_calls", "brave_searches",
                                      *WEB_RUN_COUNTERS.values())),
                ~ControlFlag.key.like("slack_delivery_%"),
            ))
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
            existing = session.get(Vendor, vendor.id)
            if existing is None:
                session.add(vendor)
            elif vendor.beneficiary_id and existing.beneficiary_id != vendor.beneficiary_id:
                # Adding a scoped sandbox beneficiary after the first boot must
                # update the canonical vendor record before any transfer run.
                existing.beneficiary_id = vendor.beneficiary_id
        if session.get(ControlFlag, "freeze") is None:
            session.add(ControlFlag(key="freeze", value="false"))
        if session.get(ControlFlag, "vultr_calls") is None:
            session.add(ControlFlag(key="vultr_calls", value="0"))
        if session.get(ControlFlag, "brave_searches") is None:
            session.add(ControlFlag(key="brave_searches", value="0"))
