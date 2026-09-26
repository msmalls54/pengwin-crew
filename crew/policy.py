from __future__ import annotations

from dataclasses import dataclass

from .db import Budget, PendingOrder, Request, Vendor


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    rule: str | None = None


def evaluate_payment(
    *,
    request: Request,
    order: PendingOrder,
    vendor: Vendor | None,
    budget: Budget | None,
    frozen: bool,
    is_admin: bool,
    already_paid: bool,
) -> PolicyDecision:
    """Check source-of-truth records. No model output is trusted as policy input."""
    if frozen:
        return PolicyDecision(False, "FREEZE")
    if already_paid:
        return PolicyDecision(False, "IDEMPOTENCY")
    if vendor is None or vendor.office_id != request.office_id:
        return PolicyDecision(False, "VENDOR_ALLOWLIST")
    if order.vendor_id != vendor.id:
        return PolicyDecision(False, "VENDOR_ALLOWLIST")
    if order.sku != request.sku:
        return PolicyDecision(False, "SKU_MISMATCH")
    if order.currency != vendor.currency:
        return PolicyDecision(False, "CURRENCY_MATCH")
    if order.qty <= 0 or request.requested_qty <= 0 or order.qty != request.requested_qty:
        return PolicyDecision(False, "QUANTITY_SANITY")
    if order.amount_cents <= 0:
        return PolicyDecision(False, "AMOUNT_INVALID")
    cap = 150000 if request.office_id == "SF" else 130000
    if order.amount_cents > cap and not is_admin:
        return PolicyDecision(False, "PER_ORDER_CAP")
    if budget is None or budget.office_id != request.office_id or budget.category != request.category:
        return PolicyDecision(False, "BUDGET_MISSING")
    if budget.limit_cents - budget.spent_cents - budget.reserved_cents < order.amount_cents:
        return PolicyDecision(False, "BUDGET")
    return PolicyDecision(True)
