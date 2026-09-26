import os
import subprocess
import sys


def test_model_selected_wrong_sku_is_blocked_then_requoted(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'sku-mismatch.db'}",
        "PAYMENT_MODE": "simulated",
        "PLANNER_MODE": "vultr",
        "SANDBOX_MODE": "local",
        "VULTR_INFERENCE_KEY": "test-only-key",
        "VULTR_MODEL": "test-only-model",
    })
    script = """
from sqlalchemy import select
from crew.db import Budget, ControlFlag, SessionLocal
from crew.inference import BuyerChoice, VultrInference
from crew.seed import seed_demo
from crew.workflow import run_purchase

def compromised_choice(self, *, requested_sku, requested_qty, page_text):
    return BuyerChoice(sku='COFFEE', quantity=1, reason='Wrong product from vendor page')

VultrInference.buyer_choice = compromised_choice
seed_demo(reset=True)
result = run_purchase(office='BER', sku='OAT-MILK', qty=6)
assert result['first_status'] == 'BLOCKED', result
assert result['blocked_rule'] == 'SKU_MISMATCH', result
assert result['corrected_status'] == 'SIMULATED', result
with SessionLocal() as session:
    budget = session.execute(select(Budget).where(Budget.office_id == 'BER', Budget.category == 'office-supplies')).scalar_one()
    assert budget.spent_cents == 6 * 320
    assert budget.reserved_cents == 0
with SessionLocal.begin() as session:
    session.get(ControlFlag, 'vultr_calls').value = '7'
seed_demo(reset=True)
with SessionLocal() as session:
    assert session.get(ControlFlag, 'vultr_calls').value == '7'
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
