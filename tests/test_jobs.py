"""Durable role queue tests; no Slack network, paid model, or provider calls."""

import os
import subprocess
import sys


def _run_script(tmp_path, name: str, script: str) -> None:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / name}",
        "PAYMENT_MODE": "simulated",
        "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST,U_OTHER",
        "SLACK_ADMIN_USER_IDS": "",
    })
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=45
    )
    assert result.returncode == 0, result.stderr


def test_four_role_run_blocks_injection_then_requotes(tmp_path):
    _run_script(tmp_path, "queue-hoodie.db", """
from sqlalchemy import select, func
from crew.seed import seed_demo
from crew.db import AgentJob, Payment, Request, SessionLocal
import crew.jobs as jobs

seed_demo(reset=True)
announcements = []
jobs._notify = lambda run_id, role, message: announcements.append((role, message))
try:
    jobs.submit_run('hoodie-attack', source_user='U_OTHER', channel_id='C_OTHER')
    assert False, 'wrong-channel request should be rejected'
except ValueError:
    pass
try:
    jobs.submit_run('hoodie-attack', source_user='U_STRANGER', channel_id='C_DEMO')
    assert False, 'unlisted user should be rejected'
except ValueError:
    pass
run_id = jobs.submit_run('hoodie-attack', source_user='U_TEST', channel_id='C_DEMO')
assert not jobs.run_one_job('Buyer')  # Concierge must dispatch first.
assert jobs.run_one_job('Concierge')
assert not jobs.run_one_job('Events')
for _ in range(20):
    if not any(jobs.run_one_job(role) for role in ('Buyer', 'Events', 'Treasurer')):
        break
run = jobs.get_run(run_id)
assert run['status'] == 'COMPLETE', run
assert [j['role'] for j in run['jobs']].count('Concierge') == 1
assert [j['role'] for j in run['jobs']].count('Buyer') == 3  # Includes corrected quote.
assert [j['role'] for j in run['jobs']].count('Treasurer') == 3
assert all(j['status'] == 'DONE' for j in run['jobs'])
with SessionLocal() as session:
    payments = session.execute(select(Payment)).scalars().all()
    assert sorted(p.status for p in payments) == ['BLOCKED', 'SIMULATED', 'SIMULATED']
    assert any(p.blocked_rule == 'QUANTITY_SANITY' for p in payments)
    requests = session.execute(select(Request)).scalars().all()
    assert len(requests) == 2
    assert all(r.source_user == 'U_TEST' for r in requests)
before = len(run['jobs'])
assert not any(jobs.run_one_job(role) for role in jobs.ROLE_KINDS)
assert len(jobs.get_run(run_id)['jobs']) == before
assert {'Concierge', 'Buyer', 'Treasurer'} <= {role for role, _ in announcements}
from crew.slack_bot import run_status_text
assert 'COMPLETE' in run_status_text(run_id, user_id='U_TEST')
assert 'No run is available' in run_status_text(run_id, user_id='U_OTHER')
""")


def test_lunch_event_role_and_uncertain_payment_hold(tmp_path):
    _run_script(tmp_path, "queue-lunch.db", """
from sqlalchemy import select
from types import SimpleNamespace
from crew.seed import seed_demo
from crew.db import AgentJob, SessionLocal, Task
import crew.jobs as jobs

seed_demo(reset=True)
jobs._notify = lambda *args: None
run_id = jobs.submit_run('welcome-kit', source_user='U_TEST', channel_id='C_DEMO')
assert jobs.run_one_job('Concierge')
assert jobs.run_one_job('Buyer')
assert jobs.run_one_job('Events')  # Lunch draft and invite, not a reservation.

# An uncertain provider outcome holds this run. No other queued transfer is tried.
jobs.pay_pending_order = lambda order_id, **kwargs: SimpleNamespace(
    id='provider-unknown', status='PROVIDER_OUTCOME_UNKNOWN', blocked_rule=None)
assert jobs.run_one_job('Treasurer')
run = jobs.get_run(run_id)
assert run['status'] == 'HELD', run
assert any(j['status'] == 'HELD' and j['role'] == 'Treasurer' for j in run['jobs'])
assert not jobs.run_one_job('Treasurer')
with SessionLocal() as session:
    event = session.execute(select(Task).where(Task.agent == 'Events', Task.kind == 'lunch')).scalar_one()
    assert 'BEGIN:VCALENDAR' in event.output_json
""")


def test_lunch_event_finishes_after_simulated_payment(tmp_path):
    _run_script(tmp_path, "queue-lunch-complete.db", """
import json
from sqlalchemy import select
from crew.seed import seed_demo
from crew.db import SessionLocal, Task
import crew.jobs as jobs

seed_demo(reset=True)
jobs._notify = lambda *args: None
run_id = jobs.submit_run('welcome-kit', source_user='U_TEST', channel_id='C_DEMO')
for _ in range(20):
    if not any(jobs.run_one_job(role) for role in
               ('Concierge', 'Buyer', 'Events', 'Treasurer')):
        break
run = jobs.get_run(run_id)
assert run['status'] == 'COMPLETE', run
assert [j['kind'] for j in run['jobs']].count('lunch_complete') == 1
assert all(j['status'] == 'DONE' for j in run['jobs'])
with SessionLocal() as session:
    event = session.execute(select(Task).where(Task.agent == 'Events', Task.kind == 'lunch')).scalar_one()
    assert json.loads(event.output_json)['payment_status'] == 'SIMULATED'
""")


def test_only_one_treasurer_job_is_claimed_per_run(tmp_path):
    _run_script(tmp_path, "queue-serial-payments.db", """
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
jobs._notify = lambda *args: None
run_id = jobs.submit_run('berlin-pantry', source_user='U_TEST', channel_id='C_DEMO')
assert jobs.run_one_job('Concierge')
assert jobs.run_one_job('Buyer')
assert jobs.run_one_job('Buyer')
first = jobs.claim_next_job('Treasurer')
assert first
assert jobs.claim_next_job('Treasurer') is None
run = jobs.get_run(run_id)
assert [j['status'] for j in run['jobs'] if j['role'] == 'Treasurer'].count('RUNNING') == 1
""")


def test_plain_english_order_uses_stated_quantities(tmp_path):
    _run_script(tmp_path, "queue-natural.db", """
from sqlalchemy import select
from crew.db import Payment, Request, SessionLocal
from crew.inference import ConciergePlan, OrderLine
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
class FakeInference:
    def concierge_plan(self, *, request_text):
        assert request_text == 'Please order 3 oat milk cartons and 2 coffee bags for Berlin.'
        return ConciergePlan(items=[
            OrderLine(sku='OAT-MILK', quantity=3, office='BER'),
            OrderLine(sku='COFFEE', quantity=2, office='BER'),
        ])
jobs.VultrInference = FakeInference
run_id = jobs.submit_run('natural-language', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Please order 3 oat milk cartons and 2 coffee bags for Berlin.')
for _ in range(20):
    if not any(jobs.run_one_job(role) for role in ('Concierge', 'Buyer', 'Treasurer')):
        break
assert jobs.get_run(run_id)['status'] == 'COMPLETE'
with SessionLocal() as session:
    requests = session.execute(select(Request).where(Request.source == 'slack')).scalars().all()
    assert sorted((r.sku, r.requested_qty) for r in requests) == [('COFFEE', 2), ('OAT-MILK', 3)]
    payments = session.execute(select(Payment)).scalars().all()
    assert [p.status for p in payments] == ['SIMULATED', 'SIMULATED']
assert any('3 oat milk cartons' in message for role, message in messages if role == 'Concierge')
assert any('demo checkout' in message for role, message in messages if role == 'Treasurer')
""")


def test_plain_english_plan_cannot_invent_quantity(tmp_path):
    _run_script(tmp_path, "queue-natural-rejected.db", """
from sqlalchemy import select
from crew.db import Payment, SessionLocal
from crew.inference import ConciergePlan, OrderLine
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append(message)
class FakeInference:
    def concierge_plan(self, *, request_text):
        return ConciergePlan(items=[OrderLine(sku='HOODIE-BER', quantity=500, office='BER')])
jobs.VultrInference = FakeInference
run_id = jobs.submit_run('natural-language', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Please order 20 hoodies for Berlin.')
assert jobs.run_one_job('Concierge')
assert jobs.get_run(run_id)['status'] == 'COMPLETE'
assert not jobs.run_one_job('Buyer')
with SessionLocal() as session:
    assert session.execute(select(Payment)).scalars().all() == []
assert any("haven't placed an order" in message for message in messages)
""")


def test_plain_english_cannot_submit_provider_payments(tmp_path):
    _run_script(tmp_path, "queue-no-provider.db", """
from types import SimpleNamespace
import crew.jobs as jobs

jobs.settings = SimpleNamespace(payment_mode="airwallex_sandbox")
try:
    jobs.submit_run("natural-language", source_user="U_TEST", channel_id="C_DEMO",
                    request_text="Order 3 oat milk cartons for Berlin")
    assert False, "English requests must not create provider payouts"
except ValueError as exc:
    assert "simulated payment mode" in str(exc)
""")
