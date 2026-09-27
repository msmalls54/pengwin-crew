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
assert 'Done.' in run_status_text(run_id, user_id='U_TEST')
assert all(not message.startswith('Run ') for _, message in announcements)
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
assert any('No real card was charged' in message for role, message in messages if role == 'Treasurer')
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


def test_stale_read_only_job_retries_once_then_holds_without_resetting_caps(tmp_path):
    _run_script(tmp_path, "queue-recover-research.db", """
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from crew.db import AgentJob, ControlFlag, SessionLocal
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((run_id, role, message))
run_id = jobs.submit_run('event-status', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='What events are live?')
assert jobs.run_one_job('Concierge')  # Dispatches a read-only Events lookup.
messages.clear()
job_id = jobs.claim_next_job('Events')
assert job_id
old = datetime.now(timezone.utc) - timedelta(minutes=20)
with SessionLocal.begin() as session:
    session.get(AgentJob, job_id).updated_at = old
    session.get(ControlFlag, 'brave_searches').value = '17'
    session.get(ControlFlag, 'vultr_calls').value = '9'

assert jobs.recover_stale_jobs('Events') == {'requeued': 1, 'held': 0}
first = jobs.get_run(run_id)
assert first['status'] == 'RUNNING'
assert first['jobs'][-1]['status'] == 'QUEUED'
assert first['jobs'][-1]['attempts'] == 1
assert jobs.claim_next_job('Events') == job_id
with SessionLocal.begin() as session:
    session.get(AgentJob, job_id).updated_at = old
assert jobs.recover_stale_jobs('Events') == {'requeued': 0, 'held': 1}
final = jobs.get_run(run_id)
assert final['status'] == 'HELD'
assert final['jobs'][-1]['status'] == 'HELD'
assert final['jobs'][-1]['attempts'] == 2
assert 'retry limit' in final['jobs'][-1]['error']
assert jobs.claim_next_job('Events') is None
assert len(messages) == 1 and messages[0][0] == run_id
with SessionLocal() as session:
    assert session.get(ControlFlag, 'brave_searches').value == '17'
    assert session.get(ControlFlag, 'vultr_calls').value == '9'
""")


def test_stale_dispatch_is_held_on_worker_sweep_and_preserves_delivery(tmp_path):
    _run_script(tmp_path, "queue-recover-dispatch.db", """
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from crew.db import AgentJob, SessionLocal, SlackDelivery
from crew.seed import seed_demo
from crew.slack_bot import run_status_text
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append(message)
run_id = jobs.submit_run('project-plan', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Plan an event at Salesforce Park next month')
job_id = jobs.claim_next_job('Concierge')
assert job_id
with SessionLocal.begin() as session:
    session.get(AgentJob, job_id).updated_at = datetime.now(timezone.utc) - timedelta(minutes=20)
    session.add(SlackDelivery(delivery_hash='d' * 64, role='Concierge', channel_id='C_DEMO',
                              source_user='U_TEST', state='DONE', run_id=run_id))

# The normal worker loop checks stale claims before looking for queued jobs.
jobs._last_recovery_sweep.clear()
assert not jobs.run_one_job('Concierge')
run = jobs.get_run(run_id)
assert run['status'] == 'HELD'
assert run['jobs'][0]['status'] == 'HELD'
assert run['jobs'][0]['attempts'] == 1
assert len(run['jobs']) == 1  # No duplicate dispatch or provider action.
assert messages and 'held it for review' in messages[0]
with SessionLocal() as session:
    delivery = session.get(SlackDelivery, 'd' * 64)
    assert (delivery.state, delivery.run_id) == ('DONE', run_id)
assert 'No run is available' in run_status_text(run_id, user_id='U_OTHER')
""")


def test_stale_payment_claim_holds_run_without_provider_retry(tmp_path):
    _run_script(tmp_path, "queue-recover-payment.db", """
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from crew.db import AgentJob, CrewRun, Payment, SessionLocal
from crew.seed import seed_demo
from sqlalchemy import select
import crew.jobs as jobs

seed_demo(reset=True)
jobs._notify = lambda *args: None
run_id, job_id = str(uuid4()), str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=run_id, flow='berlin-pantry', source_user='U_TEST',
                        channel_id='C_DEMO', status='RUNNING'))
    session.flush()
    session.add(AgentJob(id=job_id, run_id=run_id, role='Treasurer', kind='pay',
                         input_json='{}', status='QUEUED'))
assert jobs.claim_next_job('Treasurer') == job_id
with SessionLocal.begin() as session:
    session.get(AgentJob, job_id).updated_at = datetime.now(timezone.utc) - timedelta(minutes=20)
assert jobs.recover_stale_jobs('Treasurer') == {'requeued': 0, 'held': 1}
run = jobs.get_run(run_id)
assert run['status'] == 'HELD'
assert run['jobs'][0]['status'] == 'HELD'
assert 'reconciliation' in run['jobs'][0]['error']
assert jobs.claim_next_job('Treasurer') is None
with SessionLocal() as session:
    assert session.execute(select(Payment)).scalars().all() == []
""")


def test_old_read_only_attempt_cannot_finish_new_claim(tmp_path):
    _run_script(tmp_path, "queue-recover-fence.db", """
from datetime import datetime, timedelta, timezone
from crew.db import AgentJob, SessionLocal
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append(message)
run_id = jobs.submit_run('event-status', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='What events are live?')
assert jobs.run_one_job('Concierge')
messages.clear()

def interrupted_perform(job, run):
    old = datetime.now(timezone.utc) - timedelta(minutes=20)
    with SessionLocal.begin() as session:
        session.get(AgentJob, job.id).updated_at = old
    assert jobs.recover_stale_jobs('Events') == {'requeued': 1, 'held': 0}
    assert jobs.claim_next_job('Events') == job.id
    return {'count': 0}, [], 'Old worker finished too late', False

jobs._perform = interrupted_perform
assert jobs.run_one_job('Events')
run = jobs.get_run(run_id)
assert run['status'] == 'RUNNING'
assert run['jobs'][-1]['status'] == 'RUNNING'
assert run['jobs'][-1]['attempts'] == 2
assert messages == []  # A superseded worker cannot announce completion/failure.
""")


def test_peer_worker_cannot_record_success_after_run_is_held(tmp_path):
    _run_script(tmp_path, "queue-recover-peer.db", """
from datetime import datetime, timedelta, timezone
from uuid import uuid4
from crew.db import AgentJob, CrewRun, SessionLocal
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
run_id, pay_id, events_id = str(uuid4()), str(uuid4()), str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=run_id, flow='welcome-kit', source_user='U_TEST',
                        channel_id='C_DEMO', status='RUNNING'))
    session.flush()
    session.add(AgentJob(id=pay_id, run_id=run_id, role='Treasurer', kind='pay',
                         input_json='{}', status='QUEUED'))
    session.add(AgentJob(id=events_id, run_id=run_id, role='Events',
                         kind='lunch_complete', input_json='{}', status='QUEUED'))
assert jobs.claim_next_job('Treasurer') == pay_id
assert jobs.claim_next_job('Events') == events_id
with SessionLocal.begin() as session:
    session.get(AgentJob, pay_id).updated_at = datetime.now(timezone.utc) - timedelta(minutes=20)
assert jobs.recover_stale_jobs('Treasurer') == {'requeued': 0, 'held': 1}

# Simulate the already-claimed Events worker returning after the run was held.
jobs.claim_next_job = lambda role: events_id
jobs._perform = lambda job, run: ({'result': 'late'}, [], 'Late result', False)
assert jobs.run_one_job('Events')
run = jobs.get_run(run_id)
assert run['status'] == 'HELD'
assert [job['status'] for job in run['jobs']] == ['HELD', 'HELD']
assert not any(message == 'Late result' for _, message in messages)
""")
