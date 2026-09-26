"""One approved, idempotent Airwallex sandbox integration check.

The request and order IDs are fixed for the September 26 demo approval. A
second invocation reads the existing Payment and cannot submit a new payout.
The Slack workers remain in simulated-payment mode.
"""

from __future__ import annotations

import json

import httpx

from .config import settings
from .db import PendingOrder, Request, SessionLocal
from .payments import AirwallexSandboxProvider, pay_pending_order


REQUEST_ID = "3d4caeb8-239e-437a-8c37-43970958419d"
ORDER_ID = "93744f8e-e1e8-4a53-89e1-5857772d21a8"
AMOUNT_CENTS = 1920


def main() -> None:
    if settings.payment_mode != "airwallex_sandbox":
        raise RuntimeError("Run only with PAYMENT_MODE=airwallex_sandbox")
    with SessionLocal.begin() as session:
        existing = session.get(Request, REQUEST_ID)
        if existing is None:
            session.add(Request(
                id=REQUEST_ID, source="operator", source_user="approved-demo-check",
                text="Approved sandbox integration check: 6 fictional oat milk cartons",
                office_id="BER", category="office-supplies", sku="OAT-MILK",
                requested_qty=6, status="NEW",
            ))
            session.flush()
            session.add(PendingOrder(
                id=ORDER_ID, request_id=REQUEST_ID, vendor_id="kaffee-kontor",
                sku="OAT-MILK", qty=6, amount_cents=AMOUNT_CENTS,
                currency="EUR", status="PENDING",
            ))
        else:
            order = session.get(PendingOrder, ORDER_ID)
            if (existing.sku, existing.requested_qty, existing.office_id) != ("OAT-MILK", 6, "BER") or not order:
                raise RuntimeError("Approved sandbox check IDs have conflicting records")
            if (order.vendor_id, order.amount_cents, order.currency) != ("kaffee-kontor", AMOUNT_CENTS, "EUR"):
                raise RuntimeError("Approved sandbox order no longer matches approval")

    payment = pay_pending_order(ORDER_ID)
    result = {
        "request_id": REQUEST_ID,
        "payment_id": payment.id,
        "amount": "EUR 19.20",
        "status": payment.status,
        "transfer_id": payment.provider_ref,
    }
    if payment.provider_ref:
        provider = AirwallexSandboxProvider()
        try:
            response = httpx.get(
                f"{provider.base}/api/v1/transfers/{payment.provider_ref}",
                headers=provider._headers(), timeout=20,
            )
            response.raise_for_status()
            result["provider_status"] = response.json().get("status")
        except (httpx.HTTPError, ValueError) as exc:
            result["receipt_check_error"] = type(exc).__name__
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
