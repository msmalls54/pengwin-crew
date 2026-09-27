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


def test_orphaned_intake_gets_one_generic_thread_notice_without_replaying_request(tmp_path):
    _run('''
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from sqlalchemy import func, select
from crew import jobs
from crew.db import AgentJob, ConversationTurn, CrewRun, SessionLocal, SlackDelivery, init_db
from crew.intake_recovery import recover_pending_intake
from crew.memory import cache_reply, enqueue_delivery_run, record_inbound

init_db()
delivery_id = 'event:before-queue-crash'
claim = record_inbound(role='Concierge', user_id='U_A', channel_id='C_DEMO',
    delivery_id=delivery_id, message_ts='100.001', thread_root_ts='100.001',
    text='Send 24 bottles to 123 Main Street, email secret@example.com')
assert claim.accepted
original = jobs._new_job
jobs._new_job = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError('crash'))
try:
    enqueue_delivery_run(flow='project-plan', user_id='U_A', channel_id='C_DEMO',
        delivery_id=delivery_id, request_text='Plan a park event and 24 bottles')
    assert False
except RuntimeError:
    pass
finally:
    jobs._new_job = original
with SessionLocal.begin() as session:
    row = session.get(SlackDelivery, sha256(delivery_id.encode()).hexdigest())
    assert row.state == 'PENDING' and row.run_id is None
    row.updated_at = datetime.now(timezone.utc) - timedelta(minutes=16)
posts = []
def post(**options):
    posts.append(options)
    assert options['channel'] == 'C_DEMO' and options['thread_ts'] == '100.001'
    assert '24 bottles' not in options['text']
    assert 'Main Street' not in options['text']
    assert 'secret@example.com' not in options['text']
    return {'ts':'100.002'}
assert recover_pending_intake('Concierge', post) == 1
assert recover_pending_intake('Concierge', post) == 0
assert len(posts) == 1
old = record_inbound(role='Concierge', user_id='U_A', channel_id='C_DEMO',
    delivery_id=delivery_id, message_ts='100.001', text='redelivered original')
assert not old.accepted and old.state == 'NEEDS_RESEND'
assert not cache_reply(delivery_id=delivery_id, user_id='U_A', channel_id='C_DEMO',
    reply='late handler must not post')
duplicate = enqueue_delivery_run(flow='project-plan', user_id='U_A', channel_id='C_DEMO',
    delivery_id=delivery_id, request_text='Plan a park event and 24 bottles')
assert duplicate.duplicate and duplicate.run_id is None
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 0
    assert session.scalar(select(func.count()).select_from(AgentJob)) == 0
    out = session.execute(select(ConversationTurn).where(
        ConversationTurn.delivery_hash == sha256(delivery_id.encode()).hexdigest(),
        ConversationTurn.direction == 'out')).scalar_one()
    assert out.content == posts[0]['text']
    assert out.message_ts == '100.002'
''', _env(tmp_path))


def test_failed_notice_is_not_retried_after_restart_and_fresh_message_can_queue(tmp_path):
    env = _env(tmp_path)
    _run('''
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from crew.db import SessionLocal, SlackDelivery, init_db
from crew.intake_recovery import recover_pending_intake
from crew.memory import record_inbound

init_db()
old_id = 'event:failed-notice'
record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id=old_id, message_ts='200.001', thread_root_ts='200.001',
    text='Please plan the event')
with SessionLocal.begin() as session:
    session.get(SlackDelivery, sha256(old_id.encode()).hexdigest()).updated_at = (
        datetime.now(timezone.utc) - timedelta(minutes=16))
attempts = []
def post(**options):
    attempts.append(options)
    raise TimeoutError('Slack outcome unknown')
assert recover_pending_intake('Events', post) == 0
assert len(attempts) == 1
with SessionLocal() as session:
    row = session.get(SlackDelivery, sha256(old_id.encode()).hexdigest())
    assert row.state == 'NEEDS_RESEND' and row.run_id is None
''', env)
    _run('''
from hashlib import sha256
from sqlalchemy import func, select
from crew.db import CrewRun, SessionLocal, SlackDelivery
from crew.intake_recovery import recover_pending_intake
from crew.memory import enqueue_delivery_run, record_inbound

def forbidden_post(**kwargs):
    raise AssertionError('a restart must not post a second notice')
assert recover_pending_intake('Events', forbidden_post) == 0
assert not record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:failed-notice', message_ts='200.001',
    text='old Slack redelivery').accepted
fresh = record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:fresh-user-resend', message_ts='200.003',
    thread_root_ts='200.001', text='Please plan the event')
assert fresh.accepted
queued = enqueue_delivery_run(flow='project-plan', user_id='U_A', channel_id='C_DEMO',
    delivery_id='event:fresh-user-resend', request_text='Please plan the event',
    thread_root_ts='200.001')
assert queued.run_id and not queued.duplicate
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == 1
    assert session.get(SlackDelivery, sha256(b'event:failed-notice').hexdigest()).state == 'NEEDS_RESEND'
    assert session.get(SlackDelivery, sha256(b'event:fresh-user-resend').hexdigest()).state == 'QUEUED'
''', env)


def test_recovery_never_notifies_another_role_or_revoked_user(tmp_path):
    _run('''
import os
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from crew.db import SessionLocal, SlackDelivery, init_db
from crew.intake_recovery import recover_pending_intake
from crew.memory import record_inbound

init_db()
key = 'event:scope-revoked'
record_inbound(role='Events', user_id='U_A', channel_id='C_DEMO',
    delivery_id=key, message_ts='300.001', thread_root_ts='300.001',
    text='Find my saved event')
with SessionLocal.begin() as session:
    session.get(SlackDelivery, sha256(key.encode()).hexdigest()).updated_at = (
        datetime.now(timezone.utc) - timedelta(minutes=16))
def forbidden_post(**kwargs):
    raise AssertionError('No notification may be sent to this role or user')
assert recover_pending_intake('Buyer', forbidden_post) == 0
with SessionLocal() as session:
    assert session.get(SlackDelivery, sha256(key.encode()).hexdigest()).state == 'PENDING'
os.environ['SLACK_ALLOWED_USER_IDS'] = 'U_B'
assert recover_pending_intake('Events', forbidden_post) == 0
with SessionLocal() as session:
    assert session.get(SlackDelivery, sha256(key.encode()).hexdigest()).state == 'NEEDS_RESEND'
''', _env(tmp_path))


def test_existing_delivery_table_gains_recovery_index(tmp_path):
    _run('''
from crew.db import engine, init_db
init_db()
with engine.begin() as connection:
    connection.exec_driver_sql('DROP INDEX ix_slack_delivery_recovery')
    before = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_slack_delivery_recovery'").all()
assert not before
init_db()
with engine.begin() as connection:
    after = connection.exec_driver_sql("SELECT name FROM sqlite_master WHERE type='index' AND name='ix_slack_delivery_recovery'").all()
assert len(after) == 1
''', _env(tmp_path))


def test_latest_project_facts_are_restart_durable_owner_scoped_and_revision_safe(tmp_path):
    env = _env(tmp_path)
    _run('''
import json
from uuid import uuid4
from crew.db import AgentJob, CrewRun, SessionLocal, init_db
from crew.memory import create_or_update_project
init_db()
plan = {
    'name': 'SF launch',
    'event': {'title': 'SF launch', 'date_phrase': 'Oct 2',
              'venue_name': 'Salesforce Park', 'capacity': 50},
    'swag': {'quantity': 40, 'product': 'water_bottle'},
    'invitations': {'emails': ['private@example.com'], 'audience_phrase': 'the team'},
}
def make_run(owner, root, plan):
    run_id = str(uuid4())
    with SessionLocal.begin() as session:
        session.add(CrewRun(id=run_id, flow='project-plan', source_user=owner,
                            channel_id='C_DEMO', status='COMPLETE'))
    create_or_update_project(run_id=run_id, owner_user_id=owner,
        channel_id='C_DEMO', thread_root_ts=root, name=plan['name'],
        safe_plan=plan, status='ACTIVE')
    return run_id
owner_run = make_run('U_A', '100.001', plan)
other_plan = {'name':'Other user project', 'event': {'title':'Private party'}}
other_run = make_run('U_B', '200.001', other_plan)
with SessionLocal.begin() as session:
    outputs = {
        'event_research': {'venue_status':'UNCONFIRMED',
                           'eventbrite_status':'NOT_CREATED',
                           'invitation_status':'DRAFT_ONLY',
                           'official_reservation_route': {
                               'operator':'Transbay Joint Powers Authority',
                               'url':'https://www.tjpa.org/permits-reservations',
                               'availability':'UNCHECKED'},
                           'invitation_draft':'Contact private@example.com'},
        'product_source': {'publisher_status':'ok', 'source_url':
                           'https://www.printful.com/custom-water-bottles',
                           'currency':'USD', 'quantity':40,
                           'unit_min':13, 'unit_max':16,
                           'subtotal_min':520, 'subtotal_max':640,
                           'checked_at':'2026-09-27T12:00:00+00:00',
                           'checkout_status':'NOT_READY'},
        'budget_review': {'review_status':'ESTIMATE_ONLY',
                          'product_subtotal_min':520, 'product_subtotal_max':640,
                          'payment_status':'NONE', 'reserved_cents':0},
    }
    for kind, output in outputs.items():
        session.add(AgentJob(id=str(uuid4()), run_id=owner_run,
            role={'event_research':'Events','product_source':'Buyer',
                  'budget_review':'Treasurer'}[kind], kind=kind,
            input_json='{}', output_json=json.dumps(output), status='DONE'))
''', env)
    _run('''
from crew.memory import (format_project_facts, latest_project_facts,
                         is_project_recall_request)
facts = latest_project_facts(user_id='U_A', channel_id='C_DEMO',
                             thread_root_ts='100.001')
assert facts['name'] == 'SF launch'
reply = format_project_facts(facts)
assert 'Salesforce Park' in reply and '40 water bottles' in reply
assert '$13.00–$16.00' in reply and '$520.00–$640.00' in reply
assert 'www.printful.com/custom-water-bottles' in reply
assert 'www.tjpa.org/permits-reservations' in reply
assert 'Checkout is not ready' in reply and 'no funds reserved' in reply
assert 'private@example.com' not in reply and 'Contact private' not in reply
assert 'Other user project' not in reply
assert latest_project_facts(user_id='U_A', channel_id='C_OTHER') is None
assert latest_project_facts(user_id='U_B', channel_id='C_DEMO')['name'] == 'Other user project'
assert is_project_recall_request('What have we planned?')
assert is_project_recall_request('What happened with my project?')
assert is_project_recall_request('Did we pay for the bottles?')
assert is_project_recall_request('Were the invitations sent?')
assert is_project_recall_request('What is the latest saved project in this thread, and what is the 24-bottle estimate? Please do not order.')
assert is_project_recall_request('What have we spent on the bottles for this project? Please distinguish the estimate from any payment.')
assert not is_project_recall_request('How much did Pengwin spend today?')
assert not is_project_recall_request('Please buy 24 bottles for the latest saved project.')
assert not is_project_recall_request('What events are live now?')
''', env)
    _run('''
from uuid import uuid4
from crew.db import CrewRun, SessionLocal
from crew.memory import create_or_update_project, format_project_facts, latest_project_facts
new_run = str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=new_run, flow='project-plan', source_user='U_A',
                        channel_id='C_DEMO', status='QUEUED'))
create_or_update_project(run_id=new_run, owner_user_id='U_A', channel_id='C_DEMO',
    thread_root_ts='100.001', name='SF launch revision',
    safe_plan={'swag':{'quantity':50, 'product':'water_bottle'}}, status='ACTIVE')
facts = latest_project_facts(user_id='U_A', channel_id='C_DEMO',
                             thread_root_ts='100.001')
reply = format_project_facts(facts)
assert facts['name'] == 'SF launch revision'
assert '50 water bottles' in reply and 'no current official product range' in reply
assert '$520.00' not in reply and '13.00' not in reply
''', env)
