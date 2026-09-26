from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


class Office(Base):
    __tablename__ = "offices"
    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))


class Budget(Base):
    __tablename__ = "budgets"
    __table_args__ = (UniqueConstraint("office_id", "category", name="uq_budget_office_category"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    office_id: Mapped[str] = mapped_column(ForeignKey("offices.id"))
    category: Mapped[str] = mapped_column(String(40))
    limit_cents: Mapped[int] = mapped_column(Integer)
    spent_cents: Mapped[int] = mapped_column(Integer, default=0)
    reserved_cents: Mapped[int] = mapped_column(Integer, default=0)


class Vendor(Base):
    __tablename__ = "vendors"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    office_id: Mapped[str] = mapped_column(ForeignKey("offices.id"))
    name: Mapped[str] = mapped_column(String(100))
    currency: Mapped[str] = mapped_column(String(3))
    kind: Mapped[str] = mapped_column(String(20))
    beneficiary_id: Mapped[str | None] = mapped_column(String(120), nullable=True)


class Request(Base):
    __tablename__ = "requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(20))
    source_user: Mapped[str] = mapped_column(String(100))
    text: Mapped[str] = mapped_column(Text)
    office_id: Mapped[str] = mapped_column(ForeignKey("offices.id"))
    category: Mapped[str] = mapped_column(String(40))
    sku: Mapped[str] = mapped_column(String(50))
    requested_qty: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="NEW")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"))
    agent: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    input_json: Mapped[str] = mapped_column(Text, default="{}")
    output_json: Mapped[str] = mapped_column(Text, default="{}")


class CrewRun(Base):
    __tablename__ = "crew_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    flow: Mapped[str] = mapped_column(String(40))
    source_user: Mapped[str] = mapped_column(String(100))
    channel_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="QUEUED")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentJob(Base):
    __tablename__ = "agent_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("crew_runs.id"), index=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    input_json: Mapped[str] = mapped_column(Text)
    output_json: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class PendingOrder(Base):
    __tablename__ = "pending_orders"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"))
    vendor_id: Mapped[str] = mapped_column(ForeignKey("vendors.id"))
    sku: Mapped[str] = mapped_column(String(50))
    qty: Mapped[int] = mapped_column(Integer)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    status: Mapped[str] = mapped_column(String(30), default="PENDING")
    vendor_order_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    pending_order_id: Mapped[str] = mapped_column(ForeignKey("pending_orders.id"), unique=True)
    request_id: Mapped[str] = mapped_column(ForeignKey("requests.id"))
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    method: Mapped[str] = mapped_column(String(40))
    status: Mapped[str] = mapped_column(String(40))
    provider_ref: Mapped[str | None] = mapped_column(String(120), nullable=True)
    blocked_rule: Mapped[str | None] = mapped_column(String(60), nullable=True)
    provider_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    request_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent: Mapped[str] = mapped_column(String(30))
    action: Mapped[str] = mapped_column(String(80))
    detail_json: Mapped[str] = mapped_column(Text, default="{}")
    severity: Mapped[str] = mapped_column(String(12), default="info")


class ControlFlag(Base):
    __tablename__ = "control_flags"
    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    value: Mapped[str] = mapped_column(String(100))


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    Base.metadata.create_all(engine)
