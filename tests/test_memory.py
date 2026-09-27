"""Durable Slack context and intake boundaries, without a live provider."""

import os
import subprocess
import sys


def _run(script: str, env: dict[str, str]) -> None:
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def _env(tmp_path) -> dict[str, str]:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'memory.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_A,U_B", "SLACK_ADMIN_USER_IDS": "",
    })
    return env


def test_redacted_context_survives_process_restart_and_stays_scoped(tmp_path):
    env = _env(tmp_path)
    _run('''
from crew.db import init_db
from crew.memory import record_inbound, record_outbound, cache_reply, redact_text
from crew.db import SessionLocal, SlackDelivery
from datetime import datetime, timedelta, timezone
from hashlib import sha256
init_db()
assert '[address redacted]' in redact_text('Deliver to 123 Main Street apartment 4 Springfield, California 12345')
claim = record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:one', message_ts='100.001',
    text='Which events are live? Invite alice@example.com via https://example.com/private?token=abc')
assert claim.accepted and not claim.duplicate
cache_reply(delivery_id='event:one', user_id='U_A', channel_id='C_DEMO',
    reply='I will check the saved event facts.')
record_outbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:one', message_ts='100.002', reply='I will check the saved event facts.')
assert not record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:one', message_ts='100.001', text='duplicate').accepted
stale = record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:stale', message_ts='100.003', text='Find my event')
assert stale.accepted
cache_reply(delivery_id='event:stale', user_id='U_A', channel_id='C_DEMO',
    reply='Cached answer')
with SessionLocal.begin() as session:
    row = session.get(SlackDelivery, sha256(b'event:stale').hexdigest())
    row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=5)
recovered = record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:stale', message_ts='100.003', text='Find my event')
assert recovered.accepted and recovered.cached_reply == 'Cached answer'
''', env)
    _run('''
from crew.memory import recent_turns, format_recent_turns
own = recent_turns(role='Events', user_id='U_A', channel_id='C_DEMO')
assert len(own) == 3, own
context = format_recent_turns(own)
assert 'USER: Which events are live?' in context
assert 'PENGWIN Events: I will check' in context
assert 'alice@example.com' not in context and 'https://example.com' not in context
assert '[email redacted]' in context and '[link redacted]' in context
assert recent_turns(role='Events', user_id='U_B', channel_id='C_DEMO') == []
assert recent_turns(role='Events', user_id='U_A', channel_id='C_OTHER') == []
assert recent_turns(role='Buyer', user_id='U_A', channel_id='C_DEMO') == []
''', env)


def test_delivery_and_run_are_atomic_and_links_are_scoped(tmp_path):
    _run('''
import json
from sqlalchemy import select, func
from crew import jobs
from crew.db import (AgentJob, ConversationTurn, CrewProject, CrewRun,
    ProjectRunLink, RunResourceLink, SessionLocal, SlackDelivery, init_db)
from crew.memory import (create_or_update_project, enqueue_delivery_run,
    link_run_resource, record_inbound)
init_db()
claim = record_inbound(role='Concierge', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:queue', message_ts='101.001', text='Calculate 2 plus 2')
assert claim.accepted
queued = enqueue_delivery_run(flow='code-task', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:queue', request_text='Calculate 2 plus 2', reply_text='On it.',
    thread_root_ts='101.001')
assert queued.run_id and not queued.duplicate
repeat = enqueue_delivery_run(flow='code-task', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:queue', request_text='Calculate 2 plus 2', reply_text='On it.')
assert repeat.duplicate and repeat.run_id == queued.run_id and repeat.cached_reply == 'On it.'
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 1
    assert session.scalar(select(func.count()).select_from(AgentJob)) == 1
    assert session.scalar(select(func.count()).select_from(ConversationTurn)) == 1
    assert session.get(SlackDelivery, __import__('hashlib').sha256(b'event:queue').hexdigest()).state == 'QUEUED'

record_inbound(role='Concierge', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:recover', message_ts='102.001', thread_root_ts='101.001',
    text='Calculate 3 plus 3')
original = jobs._new_job
def fail_before_commit(*args, **kwargs):
    raise RuntimeError('injected transaction failure')
jobs._new_job = fail_before_commit
try:
    enqueue_delivery_run(flow='code-task', user_id='U_A', channel_id='C_DEMO',
        delivery_id='event:recover', request_text='Calculate 3 plus 3')
    assert False, 'injected failure should propagate'
except RuntimeError:
    pass
finally:
    jobs._new_job = original
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 1
    key = __import__('hashlib').sha256(b'event:recover').hexdigest()
    assert session.get(SlackDelivery, key).state == 'PENDING'
recovered = enqueue_delivery_run(flow='code-task', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:recover', request_text='Calculate 3 plus 3',
    thread_root_ts='101.001')
assert recovered.run_id and recovered.run_id != queued.run_id

project_id = create_or_update_project(run_id=queued.run_id, owner_user_id='U_A',
    channel_id='C_DEMO', name='Office launch',
    safe_plan={'summary':'Ask alice@example.com', 'meeting_url':'https://example.com/secret',
               'guest_count': 40},
    thread_root_ts='101.001', status='ACTIVE')
assert project_id == queued.run_id
amended_project_id = create_or_update_project(run_id=recovered.run_id,
    owner_user_id='U_A', channel_id='C_DEMO', name='Office launch amendment',
    safe_plan={'bottles': 40}, thread_root_ts='101.001', status='PLANNING')
assert amended_project_id == project_id
link_run_resource(run_id=queued.run_id, role='Events', resource_type='eventbrite_event',
    resource_id='12345', safe_facts={'status':'published', 'api_token':'private'},
    visibility='channel_public')
link_run_resource(run_id=queued.run_id, role='Events', resource_type='eventbrite_event',
    resource_id='12345', safe_facts={'status':'published'}, visibility='channel_public')
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(ProjectRunLink)) == 2
    assert session.scalar(select(func.count()).select_from(RunResourceLink)) == 1
    project = session.get(CrewProject, project_id)
    assert project.status == 'PLANNING' and project.name == 'Office launch amendment'
    assert json.loads(project.plan_json) == {'bottles': 40}
try:
    create_or_update_project(run_id=queued.run_id, owner_user_id='U_B',
        channel_id='C_DEMO', name='wrong owner')
    assert False, 'other member must not change the project'
except ValueError:
    pass
''', _env(tmp_path))
