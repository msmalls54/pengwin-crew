from __future__ import annotations

import json

from sqlalchemy.orm import Session

from .db import AuditEvent


def record(session: Session, *, agent: str, action: str, request_id: str | None = None,
           detail: dict | None = None, severity: str = "info") -> None:
    session.add(AuditEvent(agent=agent, action=action, request_id=request_id,
                           detail_json=json.dumps(detail or {}, sort_keys=True), severity=severity))
