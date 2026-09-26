"""Create three fictional Airwallex sandbox contacts for the demo stores.

Run only on the IP-allowlisted control VM with the sandbox-scoped key. This
module creates contacts, not transfers. It is idempotent by company name.
"""

from __future__ import annotations

import json

import httpx

from .payments import AirwallexSandboxProvider


CONTACTS = {
    "AW_BENEFICIARY_KAFFEE": {
        "company_name": "Kaffee Kontor Demo GmbH",
        "bank_details": {
            "account_currency": "EUR", "account_name": "Kaffee Kontor Demo GmbH",
            "iban": "DE89370400440532013000", "bank_country_code": "DE",
        },
        "address": {
            "city": "Berlin", "country_code": "DE", "postcode": "10117",
            "street_address": "Demo Street 1",
        },
    },
    "AW_BENEFICIARY_DRUCKWERK": {
        "company_name": "Druckwerk Demo GmbH",
        "bank_details": {
            "account_currency": "EUR", "account_name": "Druckwerk Demo GmbH",
            "iban": "DE62370400440532013001", "bank_country_code": "DE",
        },
        "address": {
            "city": "Berlin", "country_code": "DE", "postcode": "10117",
            "street_address": "Demo Street 2",
        },
    },
    "AW_BENEFICIARY_BAY_SUPPLY": {
        "company_name": "Bay Supply Demo LLC",
        "bank_details": {
            "account_currency": "USD", "account_name": "Bay Supply Demo LLC",
            "account_number": "50001121", "account_routing_type1": "aba",
            "account_routing_value1": "021000021", "bank_country_code": "US",
            "bank_account_category": "Checking", "local_clearing_system": "ACH",
        },
        "address": {
            "city": "San Francisco", "country_code": "US", "postcode": "94103",
            "state": "California", "street_address": "Demo Street 3",
        },
    },
}


def main() -> None:
    provider = AirwallexSandboxProvider()
    headers = provider._headers()
    response = httpx.get(f"{provider.base}/api/v1/beneficiaries", headers=headers, timeout=20)
    response.raise_for_status()
    existing = {
        item.get("beneficiary", {}).get("company_name"): item["id"]
        for item in response.json().get("items", [])
    }
    result = {}
    for key, contact in CONTACTS.items():
        name = contact["company_name"]
        if name in existing:
            result[key] = existing[name]
            continue
        payload = {
            "beneficiary": {"type": "BANK_ACCOUNT", "entity_type": "COMPANY", **contact},
            "transfer_methods": ["LOCAL"],
        }
        created = httpx.post(f"{provider.base}/api/v1/beneficiaries/create",
                             headers=headers, json=payload, timeout=25)
        if created.status_code != 201:
            error = created.json() if created.headers.get("content-type", "").startswith("application/json") else {}
            fields = [
                {"source": item.get("source"), "code": item.get("code")}
                for item in error.get("details", {}).get("errors", [])
            ]
            raise RuntimeError(
                f"{key} rejected: HTTP {created.status_code}, "
                f"code={error.get('code', 'unknown')}, fields={fields}"
            )
        result[key] = created.json()["id"]
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
