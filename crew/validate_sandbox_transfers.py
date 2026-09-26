"""Validate sample sandbox transfers without creating or submitting them."""

from __future__ import annotations

from uuid import uuid4

import httpx

from .db import SessionLocal, Vendor
from .payments import AirwallexSandboxProvider


SAMPLES = (
    ("kaffee-kontor", 1920, "EUR"),
    ("druckwerk", 4800, "EUR"),
    ("bay-supply", 140000, "USD"),
)


def main() -> None:
    provider = AirwallexSandboxProvider()
    with SessionLocal() as session:
        beneficiary_ids = {
            vendor_id: session.get(Vendor, vendor_id).beneficiary_id
            for vendor_id, _, _ in SAMPLES
        }
    for vendor_id, amount_cents, currency in SAMPLES:
        body = provider.transfer_body(
            payment_id=str(uuid4()), amount_cents=amount_cents,
            currency=currency, beneficiary_id=beneficiary_ids[vendor_id],
        )
        response = httpx.post(
            f"{provider.base}/api/v1/transfers/validate",
            headers=provider._headers(), json=body, timeout=30,
        )
        if response.status_code != 200:
            error = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
            fields = [
                {"source": item.get("source"), "code": item.get("code")}
                for item in error.get("details", {}).get("errors", [])
            ]
            raise RuntimeError(
                f"{vendor_id} validation rejected: HTTP {response.status_code}, "
                f"code={error.get('code', 'unknown')}, fields={fields}"
            )
        print(f"{vendor_id}: sandbox transfer payload accepted by validation API ({currency} {amount_cents / 100:.2f})")


if __name__ == "__main__":
    main()
