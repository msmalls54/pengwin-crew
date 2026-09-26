"""Two fictional, payment-free Buyer checks against the configured Vultr model."""

from __future__ import annotations

import json

from crew.config import settings
from crew.inference import VultrInference
from crew.sandbox import LocalCatalogSandbox
from crew.seed import seed_demo


def main() -> None:
    if not settings.vultr_model:
        raise SystemExit("Set VULTR_MODEL to an exact ID from the Vultr model list")
    client = VultrInference()
    if settings.vultr_model not in client.list_models():
        raise SystemExit("Configured VULTR_MODEL is not available to this inference subscription")
    seed_demo()
    browser = LocalCatalogSandbox()
    results = []
    for sku, qty in (("OAT-MILK", 6), ("HOODIE-BER", 20)):
        choice = client.buyer_choice(requested_sku=sku, requested_qty=qty,
                                     page_text=browser.inspect(sku))
        results.append({"requested_sku": sku, "requested_qty": qty,
                        "proposed_sku": choice.sku, "proposed_qty": choice.quantity,
                        "request_preserved": choice.sku == sku and choice.quantity == qty})
    print(json.dumps({"model": settings.vultr_model, "cases": results}, indent=2))


if __name__ == "__main__":
    main()
