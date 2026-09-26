"""Synthetic Orderly-shaped fixtures; no DoorDash CLI or credentials involved."""

from copy import deepcopy

import pytest

from crew.doordash import DoorDashPreviewRejected, ExpectedItem, validate_saved_preview


def responses():
    cart = {"structuredContent": {
        "success": True, "cart_uuid": "cart_test_abcdefgh",
        "cart": {"id": "cart_test_abcdefgh", "store_name": "Fictional Lunch",
                 "items": [{"item_id": "123", "name": "Lunch box", "quantity": 8}]}}}
    preview = {"structuredContent": {
        "success": True, "cart_uuid": "cart_test_abcdefgh",
        "quote": {
            "store_order_cart": {"is_consumer_pickup": False, "invalid_items": []},
            "delivery_address": {"printable_address": "100 Example St, San Francisco, CA 94105"},
            "delivery_availability": {"asap_available": True, "is_within_delivery_region": True},
            "net_total_before_tip": {"unit_amount": 17600, "display_string": "$176.00"},
            "contains_alcohol_item": False,
        }}}
    return cart, preview


def validate(cart, preview):
    return validate_saved_preview(
        cart_response=cart, preview_response=preview,
        expected_cart_uuid="cart_test_abcdefgh", expected_store_name="Fictional Lunch",
        expected_items=(ExpectedItem("123", "Lunch box", 8),),
        expected_delivery_address="100 Example St, San Francisco, CA 94105",
        tip_cents=2000, max_total_cents=20000,
    )


def test_exact_preview_is_stable_and_has_no_checkout_path():
    cart, preview = responses()
    first = validate(cart, preview)
    assert first.total_cents == 19600
    assert first.snapshot_hash == validate(cart, preview).snapshot_hash
    assert not hasattr(first, "submit")
    assert not hasattr(first, "address")


@pytest.mark.parametrize(("field", "value", "code"), [
    ("cart_qty", 80, "DOORDASH_CART_CONTENT_MISMATCH"),
    ("cart_option", [{"unexpected": "size"}], "DOORDASH_ITEM_OPTIONS_UNREVIEWED"),
    ("address", "200 Other St", "DOORDASH_DELIVERY_ADDRESS_MISMATCH"),
    ("restricted", True, "DOORDASH_RESTRICTED_ITEM"),
    ("price", 19000, "DOORDASH_TOTAL_LIMIT_EXCEEDED"),
])
def test_mismatches_fail_closed_without_echoing_provider_data(field, value, code):
    cart, preview = deepcopy(responses())
    if field == "cart_qty":
        cart["structuredContent"]["cart"]["items"][0]["quantity"] = value
    elif field == "cart_option":
        cart["structuredContent"]["cart"]["items"][0]["options"] = value
    elif field == "address":
        preview["structuredContent"]["quote"]["delivery_address"]["printable_address"] = value
    elif field == "restricted":
        preview["structuredContent"]["quote"]["contains_alcohol_item"] = value
    elif field == "price":
        preview["structuredContent"]["quote"]["net_total_before_tip"]["unit_amount"] = value
    with pytest.raises(DoorDashPreviewRejected) as error:
        validate(cart, preview)
    assert error.value.code == code
    assert str(error.value) == code


def test_changed_quote_changes_snapshot():
    cart, preview = responses()
    first = validate(cart, preview)
    preview["structuredContent"]["quote"]["net_total_before_tip"]["unit_amount"] = 17500
    assert validate(cart, preview).snapshot_hash != first.snapshot_hash
