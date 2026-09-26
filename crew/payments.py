from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4

import httpx
from sqlalchemy import select

from .audit import record
from .config import settings
from .db import Budget, ControlFlag, Payment, PendingOrder, Request, SessionLocal, Vendor
from .policy import evaluate_payment


class ProviderError(Exception):
    def __init__(self, message: str, *, definitive: bool):
        super().__init__(message)
        self.definitive = definitive


class SimulatedProvider:
    def balance_cents(self, currency: str) -> int:
        return 10_000_000

    def submit(self, *, payment_id: str, amount_cents: int, currency: str, beneficiary_id: str | None) -> tuple[str, str]:
        return f"sim-{payment_id}", "SIMULATED"


class AirwallexSandboxProvider:
    """Calls only the documented Airwallex sandbox host."""

    def __init__(self):
        if not settings.airwallex_client_id or not settings.airwallex_api_key:
            raise ProviderError("Airwallex sandbox credentials are not configured", definitive=True)
        if settings.airwallex_base.rstrip("/") != "https://api.sandbox.airwallex.com":
            raise ProviderError("Airwallex endpoint must be the sandbox host", definitive=True)
        self.base = settings.airwallex_base.rstrip("/")
        self._token: str | None = None
        self._expires_at: datetime | None = None

    def _auth(self) -> str:
        if self._token and self._expires_at and datetime.now(timezone.utc) < self._expires_at:
            return self._token
        try:
            response = httpx.post(f"{self.base}/api/v1/authentication/login", headers={
                "x-client-id": settings.airwallex_client_id,
                "x-api-key": settings.airwallex_api_key,
                "Content-Type": "application/json",
            }, timeout=20)
            response.raise_for_status()
            body = response.json()
            self._token = body["token"]
            self._expires_at = datetime.fromisoformat(body["expires_at"].replace("Z", "+00:00"))
            return self._token
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            raise ProviderError(f"Airwallex authentication failed: {type(exc).__name__}", definitive=False) from exc

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._auth()}", "Content-Type": "application/json"}

    def balance_cents(self, currency: str) -> int:
        try:
            response = httpx.get(f"{self.base}/api/v1/balances/current", headers=self._headers(), timeout=20)
            response.raise_for_status()
            balances = response.json()
            for item in balances:
                if item.get("currency") == currency and item.get("account_type", "cash").lower() == "cash":
                    return int((Decimal(str(item["available_amount"])) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
            return 0
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            raise ProviderError(f"Could not read Airwallex balance: {type(exc).__name__}", definitive=False) from exc

    def submit(self, *, payment_id: str, amount_cents: int, currency: str, beneficiary_id: str | None) -> tuple[str, str]:
        if not beneficiary_id:
            raise ProviderError("Saved sandbox beneficiary is missing", definitive=True)
        amount = str((Decimal(amount_cents) / 100).quantize(Decimal("0.01")))
        body = {
            "beneficiary_id": beneficiary_id,
            "transfer_amount": amount,
            "transfer_currency": currency,
            "source_currency": currency,
            "transfer_method": "LOCAL",
            "reason": "office supplies",
            "reference": f"CREW-{payment_id[:18]}",
            "request_id": payment_id,
        }
        try:
            response = httpx.post(f"{self.base}/api/v1/transfers/create", headers=self._headers(), json=body, timeout=30)
            response.raise_for_status()
            data = response.json()
            return str(data["id"]), str(data.get("status", "CREATED"))
        except httpx.HTTPStatusError as exc:
            definitive = 400 <= exc.response.status_code < 500 and exc.response.status_code not in (408, 409, 429)
            raise ProviderError(f"Airwallex rejected transfer: HTTP {exc.response.status_code}", definitive=definitive) from exc
        except (httpx.RequestError, KeyError, ValueError) as exc:
            raise ProviderError(f"Airwallex transfer outcome unknown: {type(exc).__name__}", definitive=False) from exc


def provider():
    if settings.payment_mode == "simulated":
        return SimulatedProvider()
    if settings.payment_mode == "airwallex_sandbox":
        return AirwallexSandboxProvider()
    raise RuntimeError("Unknown payment mode")


def pay_pending_order(order_id: str, *, is_admin: bool = False) -> Payment:
    """Reserve budget before a provider call; never retry an uncertain transfer with a new ID."""
    with SessionLocal.begin() as session:
        order = session.execute(select(PendingOrder).where(PendingOrder.id == order_id).with_for_update()).scalar_one()
        request = session.get(Request, order.request_id)
        vendor = session.get(Vendor, order.vendor_id)
        budget = session.execute(select(Budget).where(Budget.office_id == request.office_id, Budget.category == request.category).with_for_update()).scalar_one_or_none()
        previous = session.execute(select(Payment).where(Payment.pending_order_id == order_id)).scalar_one_or_none()
        if previous is not None:
            return previous
        flag = session.get(ControlFlag, "freeze")
        decision = evaluate_payment(request=request, order=order, vendor=vendor, budget=budget,
                                    frozen=settings.freeze or (flag is not None and flag.value == "true"),
                                    is_admin=is_admin, already_paid=False)
        payment = Payment(id=str(uuid4()), pending_order_id=order.id, request_id=request.id,
                          amount_cents=order.amount_cents, currency=order.currency,
                          method="airwallex_payout" if settings.payment_mode == "airwallex_sandbox" else "simulated",
                          status="BLOCKED" if not decision.allowed else "PENDING_PROVIDER",
                          blocked_rule=decision.rule, simulated=settings.payment_mode == "simulated")
        session.add(payment)
        if not decision.allowed:
            order.status = "BLOCKED"
            request.status = "BLOCKED"
            record(session, agent="Policy", action="payment_blocked", request_id=request.id,
                   detail={"rule": decision.rule, "order_id": order.id, "qty": order.qty}, severity="error")
            return payment
        if settings.payment_mode == "airwallex_sandbox" and not vendor.beneficiary_id:
            payment.status = "BLOCKED"
            payment.blocked_rule = "BENEFICIARY_MISSING"
            order.status = "BLOCKED"
            record(session, agent="Policy", action="payment_blocked", request_id=request.id,
                   detail={"rule": "BENEFICIARY_MISSING"}, severity="error")
            return payment
        try:
            pay_provider = provider()
            balance_cents = pay_provider.balance_cents(order.currency)
        except ProviderError as exc:
            payment.status = "BLOCKED"
            payment.blocked_rule = "PROVIDER_UNAVAILABLE" if exc.definitive else "BALANCE_UNVERIFIED"
            payment.provider_error = str(exc)
            record(session, agent="Policy", action="payment_blocked", request_id=request.id,
                   detail={"rule": payment.blocked_rule}, severity="error")
            return payment
        if balance_cents < order.amount_cents:
            payment.status = "BLOCKED"
            payment.blocked_rule = "BALANCE"
            record(session, agent="Policy", action="payment_blocked", request_id=request.id,
                   detail={"rule": "BALANCE", "currency": order.currency}, severity="error")
            return payment
        budget.reserved_cents += order.amount_cents
        order.status = "PAYMENT_PENDING"
        record(session, agent="Treasurer", action="payment_reserved", request_id=request.id,
               detail={"order_id": order.id, "amount_cents": order.amount_cents, "currency": order.currency})
        payment_id, request_id, amount_cents, currency, beneficiary_id = payment.id, request.id, order.amount_cents, order.currency, vendor.beneficiary_id

    try:
        provider_ref, provider_status = pay_provider.submit(payment_id=payment_id, amount_cents=amount_cents,
                                                            currency=currency, beneficiary_id=beneficiary_id)
    except ProviderError as exc:
        with SessionLocal.begin() as session:
            payment = session.get(Payment, payment_id)
            order = session.get(PendingOrder, order_id)
            request = session.get(Request, request_id)
            if exc.definitive:
                budget = session.execute(select(Budget).where(Budget.office_id == request.office_id, Budget.category == request.category).with_for_update()).scalar_one()
                budget.reserved_cents -= amount_cents
                payment.status = "PROVIDER_REJECTED"
                order.status = "REJECTED"
            else:
                payment.status = "PROVIDER_OUTCOME_UNKNOWN"
                order.status = "RECONCILE_REQUIRED"
            payment.provider_error = str(exc)
            record(session, agent="Treasurer", action="provider_error", request_id=request_id,
                   detail={"status": payment.status, "error": str(exc)}, severity="error")
            return payment

    with SessionLocal.begin() as session:
        payment = session.get(Payment, payment_id)
        order = session.get(PendingOrder, order_id)
        request = session.get(Request, request_id)
        budget = session.execute(select(Budget).where(Budget.office_id == request.office_id, Budget.category == request.category).with_for_update()).scalar_one()
        payment.provider_ref = provider_ref
        failed = settings.payment_mode != "simulated" and provider_status.upper() in {"FAILED", "CANCELLED", "APPROVAL_REJECTED"}
        payment.status = "SIMULATED" if settings.payment_mode == "simulated" else ("PROVIDER_REJECTED" if failed else "SUBMITTED_SANDBOX")
        if failed:
            budget.reserved_cents -= amount_cents
        elif settings.payment_mode == "simulated":
            budget.reserved_cents -= amount_cents
            budget.spent_cents += amount_cents
        order.status = payment.status
        request.status = payment.status
        record(session, agent="Treasurer", action="payment_submitted", request_id=request_id,
               detail={"provider_ref": provider_ref, "provider_status": provider_status,
                       "simulated": payment.simulated, "amount_cents": amount_cents, "currency": currency})
        return payment
