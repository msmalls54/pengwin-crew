import subprocess
import sys
import textwrap


def test_payment_rejects_even_one_extra_item():
    script = textwrap.dedent("""
    from crew.db import Budget, PendingOrder, Request, Vendor
    from crew.policy import evaluate_payment

    request = Request(id="r", source="test", source_user="u", text="20 hoodies",
                      office_id="BER", category="swag", sku="HOODIE-BER", requested_qty=20)
    vendor = Vendor(id="v", office_id="BER", name="Mock store", currency="EUR", kind="mock")
    budget = Budget(office_id="BER", category="swag", limit_cents=130000,
                    spent_cents=0, reserved_cents=0)
    order = PendingOrder(id="o", request_id="r", vendor_id="v", sku="HOODIE-BER",
                         qty=21, amount_cents=21 * 3200, currency="EUR")

    decision = evaluate_payment(request=request, order=order, vendor=vendor,
                                budget=budget, frozen=False, is_admin=False,
                                already_paid=False)
    assert not decision.allowed
    assert decision.rule == "QUANTITY_SANITY"
    """)
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
