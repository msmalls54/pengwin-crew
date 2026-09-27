"""A model cannot turn a negated line item into a simulated checkout."""

import os
import subprocess
import sys


def test_negated_item_is_rejected_but_affirmative_coffee_is_queued(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'intake.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "",
    })
    script = '''
from sqlalchemy import select
from crew.db import AgentJob, Payment, PendingOrder, SessionLocal
from crew.inference import ConciergePlan, OrderLine
from crew.intake import checked_plan
from crew.seed import seed_demo
import crew.jobs as jobs

request = ('Do not order 24 oat milk cartons for Berlin; '
           'buy 2 coffee bags for Berlin instead')
milk = OrderLine(sku='OAT-MILK', quantity=24, office='BER')
coffee = OrderLine(sku='COFFEE', quantity=2, office='BER')
try:
    checked_plan(ConciergePlan(items=[milk, coffee]), request)
    assert False, 'negated oat milk must not be accepted'
except ValueError as exc:
    assert 'affirmed' in str(exc)
assert checked_plan(ConciergePlan(items=[coffee]), request).items == [coffee]
assert checked_plan(ConciergePlan(items=[coffee]),
    'Do not order 24 oat milk cartons for Berlin and buy 2 coffee bags for Berlin').items == [coffee]
assert checked_plan(ConciergePlan(items=[milk]),
    'Do not order 24 oat milk cartons for Berlin; buy 24 oat milk cartons for Berlin instead').items == [milk]
assert checked_plan(ConciergePlan(items=[milk]),
    "Don't forget to order 24 oat milk cartons for Berlin").items == [milk]
try:
    checked_plan(ConciergePlan(items=[
        OrderLine(sku='OAT-MILK', quantity=2, office='BER'),
        OrderLine(sku='COFFEE', quantity=3, office='BER'),
    ]), 'Buy 3 oat milk cartons and 2 coffee bags for Berlin.')
    assert False, 'model-swapped quantities must not be accepted'
except ValueError as exc:
    assert 'affirmed' in str(exc)

seed_demo(reset=True)
jobs._notify = lambda *args: None
class UnsafeInference:
    def concierge_plan(self, *, request_text):
        assert request_text == request
        return ConciergePlan(items=[milk, coffee])
jobs.VultrInference = UnsafeInference
bad_run = jobs.submit_run('natural-language', source_user='U_TEST',
                          channel_id='C_DEMO', request_text=request)
assert jobs.run_one_job('Concierge')
with SessionLocal() as session:
    assert session.execute(select(AgentJob).where(
        AgentJob.run_id == bad_run, AgentJob.role == 'Buyer')).scalars().all() == []
    assert session.execute(select(PendingOrder)).scalars().all() == []
    assert session.execute(select(Payment)).scalars().all() == []

class SafeInference:
    def concierge_plan(self, *, request_text):
        assert request_text == request
        return ConciergePlan(items=[coffee])
jobs.VultrInference = SafeInference
safe_run = jobs.submit_run('natural-language', source_user='U_TEST',
                           channel_id='C_DEMO', request_text=request)
assert jobs.run_one_job('Concierge')
with SessionLocal() as session:
    buyer = session.execute(select(AgentJob).where(
        AgentJob.run_id == safe_run, AgentJob.role == 'Buyer')).scalars().all()
    assert len(buyer) == 1
    assert '"sku": "COFFEE"' in buyer[0].input_json
    assert session.execute(select(PendingOrder)).scalars().all() == []
    assert session.execute(select(Payment)).scalars().all() == []
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
