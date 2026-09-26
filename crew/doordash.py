"""Offline validation of DoorDash CLI cart and preview responses.

This module intentionally has no CLI runner, authentication, cart mutation, or
checkout path. It can only validate responses supplied by a separate caller.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Mapping


class DoorDashPreviewRejected(ValueError):
    """A stable code that does not echo provider data or customer information."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ExpectedItem:
    item_id: str
    name: str
    quantity: int


@dataclass(frozen=True)
class DoorDashPreview:
    cart_uuid: str
    store_name: str
    items: tuple[ExpectedItem, ...]
    total_before_tip_cents: int
    tip_cents: int
    total_cents: int
    snapshot_hash: str


def _object(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
    return value


def _string(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
    return " ".join(value.split())


def _cents(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
    return value


def _payload(response: Any) -> Mapping[str, Any]:
    envelope = _object(response)
    if envelope.get("isError") is True:
        raise DoorDashPreviewRejected("DOORDASH_ERROR_ENVELOPE")
    data = _object(envelope.get("structuredContent"))
    if data.get("success") is not True:
        raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
    return data


def validate_saved_preview(
    *,
    cart_response: Mapping[str, Any],
    preview_response: Mapping[str, Any],
    expected_cart_uuid: str,
    expected_store_name: str,
    expected_items: tuple[ExpectedItem, ...],
    expected_delivery_address: str,
    tip_cents: int,
    max_total_cents: int,
) -> DoorDashPreview:
    """Validate already obtained JSON against an exact supervised food order.

    The caller must supply an approved destination and a current total limit.
    No address, card, or raw provider response is returned or persisted.
    """
    if not expected_items or not expected_delivery_address.strip():
        raise DoorDashPreviewRejected("DOORDASH_EXPECTATION_MISSING")
    tip = _cents(tip_cents)
    limit = _cents(max_total_cents)
    cart_data = _payload(cart_response)
    preview_data = _payload(preview_response)
    cart = _object(cart_data.get("cart"))
    quote = _object(preview_data.get("quote"))
    cart_uuid = _string(cart_data.get("cart_uuid"))
    if (cart_uuid != expected_cart_uuid or _string(cart.get("id")) != cart_uuid
            or _string(preview_data.get("cart_uuid")) != cart_uuid):
        raise DoorDashPreviewRejected("DOORDASH_CART_MISMATCH")
    store_name = _string(cart.get("store_name"))
    if store_name.casefold() != _string(expected_store_name).casefold():
        raise DoorDashPreviewRejected("DOORDASH_STORE_MISMATCH")

    raw_items = cart.get("items")
    if not isinstance(raw_items, list) or not raw_items or len(raw_items) > 20:
        raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
    actual_items: list[ExpectedItem] = []
    for raw in raw_items:
        item = _object(raw)
        if any(item.get(key) for key in ("nested_options", "options", "item_options")):
            raise DoorDashPreviewRejected("DOORDASH_ITEM_OPTIONS_UNREVIEWED")
        quantity = _cents(item.get("quantity"))
        if quantity == 0:
            raise DoorDashPreviewRejected("DOORDASH_SCHEMA_MISMATCH")
        actual_items.append(ExpectedItem(_string(item.get("item_id")).removeprefix("i_"),
                                         _string(item.get("name")), quantity))
    if len({item.item_id for item in actual_items}) != len(actual_items):
        raise DoorDashPreviewRejected("DOORDASH_CART_CONTENT_MISMATCH")
    expected = sorted((item.item_id, item.name.casefold(), item.quantity) for item in expected_items)
    actual = sorted((item.item_id, item.name.casefold(), item.quantity) for item in actual_items)
    if actual != expected:
        raise DoorDashPreviewRejected("DOORDASH_CART_CONTENT_MISMATCH")

    order_cart = _object(quote.get("store_order_cart"))
    if order_cart.get("invalid_items") or order_cart.get("is_consumer_pickup") is not False:
        raise DoorDashPreviewRejected("DOORDASH_FULFILLMENT_MISMATCH")
    if str(order_cart.get("fulfillment_type", "DELIVERY")).upper() != "DELIVERY":
        raise DoorDashPreviewRejected("DOORDASH_FULFILLMENT_MISMATCH")
    if quote.get("contains_alcohol_item") is True or _cents(quote.get("min_age_requirement", 0)) > 0:
        raise DoorDashPreviewRejected("DOORDASH_RESTRICTED_ITEM")
    address = _object(quote.get("delivery_address"))
    if _string(address.get("printable_address")).casefold() != _string(expected_delivery_address).casefold():
        raise DoorDashPreviewRejected("DOORDASH_DELIVERY_ADDRESS_MISMATCH")
    availability = _object(quote.get("delivery_availability"))
    if availability.get("asap_available") is not True or availability.get("is_within_delivery_region") is False:
        raise DoorDashPreviewRejected("DOORDASH_DELIVERY_UNAVAILABLE")
    before_tip = _cents(_object(quote.get("net_total_before_tip")).get("unit_amount"))
    total = before_tip + tip
    if total > limit:
        raise DoorDashPreviewRejected("DOORDASH_TOTAL_LIMIT_EXCEEDED")

    # The hash binds the reviewed checkout fields. Never use it as a purchase
    # authorization; checkout needs a separate durable approval and fresh quote.
    snapshot = {
        "cart_uuid": cart_uuid,
        "store_name": store_name,
        "items": actual,
        "fulfillment": "DELIVERY",
        "address": _string(address.get("printable_address")),
        "total_before_tip_cents": before_tip,
        "tip_cents": tip,
        "total_cents": total,
    }
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=lambda item: vars(item)).encode()).hexdigest()
    return DoorDashPreview(cart_uuid, store_name, tuple(actual_items), before_tip, tip, total,
                           f"sha256:{digest}")
