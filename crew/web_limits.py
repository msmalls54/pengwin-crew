"""Atomic, durable run limits for the two web entry points."""

from __future__ import annotations

from sqlalchemy import Integer, String, cast, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .db import ControlFlag, CrewRun


WEB_RUN_LIMIT = 30
WEB_RUN_COUNTERS = {
    "web-demo": "web_code_runs",
    "web-admin": "web_admin_runs",
}


def _ensure_counter(session: Session, source_user: str) -> str:
    """Install a missing counter from legacy runs without replacing an existing one.

    The count is evaluated inside INSERT. Concurrent initializers serialize on
    the flag's unique key, then use its committed value instead of resetting it.
    """
    key = WEB_RUN_COUNTERS[source_user]
    count = select(func.count(CrewRun.id)).where(
        CrewRun.source_user == source_user,
    ).scalar_subquery()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgres_insert(ControlFlag)
    elif dialect == "sqlite":
        statement = sqlite_insert(ControlFlag)
    else:
        raise RuntimeError("Atomic web run limits require PostgreSQL or SQLite")
    session.execute(statement.values(
        key=key, value=cast(count, String(100)),
    ).on_conflict_do_nothing(index_elements=[ControlFlag.key]))
    return key


def initialize_web_run_counters(session: Session) -> None:
    """Capture old run counts before a demo reset can remove their rows."""
    for source_user in WEB_RUN_COUNTERS:
        _ensure_counter(session, source_user)


def reserve_web_run_slot(session: Session, source_user: str) -> None:
    """Reserve one of 30 slots in the same transaction as the new run."""
    key = _ensure_counter(session, source_user)
    value = cast(ControlFlag.value, Integer)
    reserved = session.execute(update(ControlFlag).where(
        ControlFlag.key == key, value >= 0, value < WEB_RUN_LIMIT,
    ).values(value=cast(value + 1, String(100))))
    if reserved.rowcount != 1:
        raise ValueError("Web demo run limit reached")
