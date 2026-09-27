"""Exercise Slack intake and egress without a real workspace or credentials."""

import os
import subprocess
import sys


def test_slack_allowlist_channel_queue_and_retry_guard(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'slack-test.db'}",
        "PAYMENT_MODE": "simulated",
        "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_ALLOWED",
        "SLACK_ADMIN_USER_IDS": "U_ADMIN",
    })
    script = """
from crew.seed import seed_demo
import crew.slack_bot as bot

seed_demo(reset=True)
queued = []
seen = set()
def fake_submit(flow, *, source_user, channel_id, delivery_id, request_text=None,
                reply_text=None, context='', thread_root_ts=None):
    if delivery_id in seen:
        return bot.memory.EnqueuedRun('run-123', True, reply_text)
    seen.add(delivery_id)
    queued.append((flow, source_user, channel_id, request_text))
    return bot.memory.EnqueuedRun('run-123', False, reply_text)
bot._submit_run = fake_submit
assert bot.is_slack_allowed('U_ALLOWED')
assert bot.is_slack_allowed('U_ADMIN')
assert not bot.is_slack_allowed('U_OTHER')
assert 'configured demo channel' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_OTHER', 'text': 'pantry', 'trigger_id': 't0'})
assert 'not allowed' in bot.handle_command(
    {'user_id': 'U_OTHER', 'channel_id': 'C_DEMO', 'text': 'pantry', 'trigger_id': 't0'})
assert 'admin' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO', 'text': 'freeze'})
assert 'no delivery ID' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO', 'text': 'pantry'})

command = {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO',
           'text': 'pantry', 'trigger_id': 't1'}
first = bot.handle_command(command)
assert first.startswith('On it.') and 'run-123' not in first, first
assert queued == [('berlin-pantry', 'U_ALLOWED', 'C_DEMO', None)]
assert bot.handle_command(command) == first
assert len(queued) == 1
natural = bot.handle_command({'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO',
                              'text': 'order 3 oat milks for Berlin', 'trigger_id': 't2'})
assert natural.startswith('On it.') and 'run-123' not in natural
assert queued[-1] == ('natural-language', 'U_ALLOWED', 'C_DEMO', '3 oat milks for Berlin')
assert '€' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO', 'text': 'budgets'})
assert 'frozen' in bot.handle_command(
    {'user_id': 'U_ADMIN', 'channel_id': 'C_DEMO', 'text': 'freeze'})
assert 'Spending is paused.' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO', 'text': 'status'})
assert 'unfrozen' in bot.handle_command(
    {'user_id': 'U_ADMIN', 'channel_id': 'C_DEMO', 'text': 'unfreeze'})
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr


def test_role_post_uses_own_token_and_demo_channel(monkeypatch):
    from crew import slack_outbound

    monkeypatch.setenv("SLACK_DEMO_CHANNEL_ID", "C_DEMO")
    monkeypatch.setenv("SLACK_BUYER_BOT_TOKEN", "buyer-test-token")
    sent = []

    class FakeClient:
        def __init__(self, *, token):
            assert token == "buyer-test-token"

        def chat_postMessage(self, **kwargs):
            sent.append(kwargs)
            return {"ok": True, "ts": "123.456"}

    monkeypatch.setattr(slack_outbound, "WebClient", FakeClient)
    ts = slack_outbound.post_role_update("Buyer", "C_DEMO", "Quote <@U_ALL> & done")
    assert ts == "123.456"
    assert sent[0]["text"] == "Quote &lt;@U_ALL&gt; &amp; done"
    assert sent[0]["mrkdwn"] is False
    try:
        slack_outbound.post_role_update("Buyer", "C_OTHER", "No")
        assert False, "wrong-channel post should be rejected"
    except ValueError:
        pass
    assert len(sent) == 1


def test_latest_progress_uses_only_the_requesters_runs(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'latest.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_A,U_B", "SLACK_ADMIN_USER_IDS": "",
    })
    script = '''
from crew.seed import seed_demo
from crew.jobs import submit_run, run_one_job
from crew.slack_bot import latest_run_status_text
seed_demo(reset=True)
submit_run('code-task', source_user='U_A', channel_id='C_DEMO', request_text='Calculate 2 plus 2')
assert run_one_job('Concierge')
submit_run('code-task', source_user='U_B', channel_id='C_DEMO', request_text='Calculate 3 plus 3')
assert 'working on it' in latest_run_status_text(user_id='U_A', channel_id='C_DEMO')
assert 'start shortly' in latest_run_status_text(user_id='U_B', channel_id='C_DEMO')
assert 'outside your Pengwin channel' in latest_run_status_text(user_id='U_OTHER', channel_id='C_DEMO')
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_published_event_progress_reads_like_a_result():
    # Keep Settings initialization isolated from other workflow tests.
    script = '''
from crew import jobs, slack_bot

slack_bot.is_slack_allowed = lambda user_id: True
jobs.get_run = lambda run_id: {
        "id": run_id, "source_user": "U_TEST", "flow": "eventbrite-event",
        "status": "COMPLETE", "jobs": [
            {"role": "Concierge", "kind": "dispatch", "status": "DONE", "output": {}},
            {"role": "Events", "kind": "eventbrite_publish", "status": "DONE",
             "output": {"url": "https://www.eventbrite.com/e/example-123"}},
        ],
    }
message = slack_bot.run_status_text("6a8cd6b1-e29e-4f67-b19c-6bb8d9a1de68", user_id="U_TEST")
assert message == "Done. Events published your public RSVP page:\\nhttps://www.eventbrite.com/e/example-123"
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_mentions_keep_followups_in_the_original_user_thread(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'threads.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_A,U_B", "SLACK_ADMIN_USER_IDS": "",
        "SLACK_CONCIERGE_BOT_TOKEN": "test-concierge",
        "SLACK_APP_TOKEN": "test-app",
        "SLACK_EVENTS_BOT_TOKEN": "test-events",
        "SLACK_EVENTS_APP_TOKEN": "test-events-app",
    })
    script = '''
from crew.db import init_db
from crew import dialogue, role_slack_bot, slack_bot
init_db()
class FakeApp:
    def __init__(self, *, token):
        self.events = {}
    def event(self, name):
        def decorate(fn):
            self.events[name] = fn
            return fn
        return decorate
    def command(self, name):
        return lambda fn: fn
slack_bot.App = FakeApp
role_slack_bot.App = FakeApp
contexts = []
def fake_answer(role, text, **kwargs):
    contexts.append((role, kwargs['user_id'], kwargs['context']))
    return 'Acknowledged by ' + role
dialogue.answer = fake_answer
role_slack_bot.answer = fake_answer
concierge = slack_bot.build_app().events['app_mention']
events = role_slack_bot.build_app('Events').events['app_mention']
sent = []
def say(**options):
    sent.append(options)
    return {'ts': '999.' + str(len(sent)).zfill(6)}
concierge({'user':'U_A','channel':'C_DEMO','ts':'100.001',
           'text':'<@CONCIERGE> Need 40 bottles'}, {'event_id':'E1'}, say)
assert sent[-1]['thread_ts'] == '100.001'
concierge({'user':'U_A','channel':'C_DEMO','ts':'100.003',
           'thread_ts':'100.001','text':'<@CONCIERGE> Make that 50'},
          {'event_id':'E2'}, say)
assert sent[-1]['thread_ts'] == '100.001'
assert 'USER: Need 40 bottles' in contexts[-1][2], contexts[-1]
assert 'PENGWIN Concierge: Acknowledged' in contexts[-1][2]
concierge({'user':'U_B','channel':'C_DEMO','ts':'200.001',
           'text':'<@CONCIERGE> My own project'}, {'event_id':'E3'}, say)
assert contexts[-1] == ('Concierge', 'U_B', '')
events({'user':'U_A','channel':'C_DEMO','ts':'300.001',
        'text':'<@EVENTS> Check my event'}, {'event_id':'E4'}, say)
assert sent[-1]['thread_ts'] == '300.001'
events({'user':'U_A','channel':'C_DEMO','ts':'300.003','thread_ts':'300.001',
        'text':'<@EVENTS> How about now?'}, {'event_id':'E5'}, say)
assert 'USER: Check my event' in contexts[-1][2]
assert 'Need 40 bottles' not in contexts[-1][2]
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_each_role_recalled_shared_project_facts_without_provider_work(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'shared-project.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_A,U_B", "SLACK_ADMIN_USER_IDS": "",
        "SLACK_BUYER_BOT_TOKEN": "test-buyer", "SLACK_BUYER_APP_TOKEN": "test-buyer-app",
        "SLACK_EVENTS_BOT_TOKEN": "test-events", "SLACK_EVENTS_APP_TOKEN": "test-events-app",
        "SLACK_TREASURER_BOT_TOKEN": "test-treasurer",
        "SLACK_TREASURER_APP_TOKEN": "test-treasurer-app",
    })
    script = '''
import json
from uuid import uuid4
from sqlalchemy import func, select
from crew.db import AgentJob, CrewRun, SessionLocal, init_db
from crew.memory import create_or_update_project
from crew import role_slack_bot
init_db()
run_id = str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=run_id, flow='project-plan', source_user='U_A',
                        channel_id='C_DEMO', status='COMPLETE'))
create_or_update_project(run_id=run_id, owner_user_id='U_A', channel_id='C_DEMO',
    thread_root_ts='100.001', name='SF launch', safe_plan={
        'event':{'title':'SF launch','venue_name':'Salesforce Park','date_phrase':'Oct 2'},
        'swag':{'quantity':40},
        'invitations':{'audience_phrase':'the team','emails':['private@example.com']}},
    status='ACTIVE')
with SessionLocal.begin() as session:
    for role, kind, output in (
        ('Events','event_research',{'venue_status':'UNCONFIRMED',
            'eventbrite_status':'NOT_CREATED','invitation_status':'DRAFT_ONLY'}),
        ('Buyer','product_source',{'publisher_status':'ok',
            'source_url':'https://www.printful.com/custom-water-bottles',
            'currency':'USD','unit_min':13,'unit_max':16,
            'subtotal_min':520,'subtotal_max':640,'checkout_status':'NOT_READY'}),
        ('Treasurer','budget_review',{'review_status':'ESTIMATE_ONLY',
            'payment_status':'NONE','reserved_cents':0}),
    ):
        session.add(AgentJob(id=str(uuid4()), run_id=run_id, role=role, kind=kind,
                             input_json='{}', output_json=json.dumps(output), status='DONE'))
class FakeApp:
    def __init__(self, *, token):
        self.events = {}
    def event(self, name):
        def decorate(fn):
            self.events[name] = fn
            return fn
        return decorate
role_slack_bot.App = FakeApp
def forbidden_model(*args, **kwargs):
    raise AssertionError('recall must not call a model, worker, or provider')
role_slack_bot.answer = forbidden_model
responses = []
def say(**options):
    responses.append(options)
    return {'ts': '900.' + str(len(responses)).zfill(6)}
with SessionLocal() as session:
    before_runs = session.scalar(select(func.count()).select_from(CrewRun))
    before_jobs = session.scalar(select(func.count()).select_from(AgentJob))
for index, role in enumerate(('Buyer', 'Events', 'Treasurer'), start=1):
    mention = role_slack_bot.build_app(role).events['app_mention']
    mention({'user':'U_A','channel':'C_DEMO','ts':f'{index}00.001',
             'text':f'<@{role}> What have we planned?'},
            {'event_id':f'E{index}'}, say)
    reply = responses[-1]['text']
    assert 'Salesforce Park' in reply and '40 water bottles' in reply
    assert '$520.00–$640.00' in reply and 'Invitations: draft only' in reply
    assert 'no funds reserved' in reply and 'Checkout is not ready' in reply
    assert responses[-1]['thread_ts'] == f'{index}00.001'
other = role_slack_bot.build_app('Events').events['app_mention']
other({'user':'U_B','channel':'C_DEMO','ts':'500.001',
       'text':'<@Events> What have we planned?'}, {'event_id':'E4'}, say)
assert "don't see a saved project" in responses[-1]['text']
assert 'Salesforce Park' not in responses[-1]['text']
model_contexts = []
def model_stub(role, text, **kwargs):
    model_contexts.append((role, kwargs['user_id'], kwargs['context']))
    return 'Read-only model stub'
role_slack_bot.answer = model_stub
buyer = role_slack_bot.build_app('Buyer').events['app_mention']
buyer({'user':'U_A','channel':'C_DEMO','ts':'700.001',
       'text':'<@Buyer> Tell me about the venue'}, {'event_id':'E6'}, say)
assert 'Salesforce Park' in model_contexts[-1][2]
assert 'Product-only subtotal estimate' in model_contexts[-1][2]
assert 'private@example.com' not in model_contexts[-1][2]
buyer({'user':'U_B','channel':'C_DEMO','ts':'800.001',
       'text':'<@Buyer> Tell me about the venue'}, {'event_id':'E7'}, say)
assert model_contexts[-1] == ('Buyer', 'U_B', '')
sent_count = len(responses)
other({'user':'U_A','channel':'C_OTHER','ts':'600.001',
       'text':'<@Events> What have we planned?'}, {'event_id':'E5'}, say)
assert len(responses) == sent_count
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == before_runs
    assert session.scalar(select(func.count()).select_from(AgentJob)) == before_jobs
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_buyer_and_treasurer_read_only_followups_use_scoped_24_bottle_project(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'human-followup.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_A,U_B", "SLACK_ADMIN_USER_IDS": "",
        "SLACK_BUYER_BOT_TOKEN": "test-buyer", "SLACK_BUYER_APP_TOKEN": "test-buyer-app",
        "SLACK_TREASURER_BOT_TOKEN": "test-treasurer",
        "SLACK_TREASURER_APP_TOKEN": "test-treasurer-app",
    })
    script = '''
import json
from uuid import uuid4
from sqlalchemy import func, select
from crew.db import AgentJob, CrewRun, SessionLocal, init_db
from crew.memory import create_or_update_project
from crew import role_slack_bot

init_db()
run_id = str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=run_id, flow='project-plan', source_user='U_A',
                        channel_id='C_DEMO', status='COMPLETE'))
create_or_update_project(run_id=run_id, owner_user_id='U_A', channel_id='C_DEMO',
    thread_root_ts='100.001', name='Salesforce Park gathering', safe_plan={
        'event':{'title':'SF gathering','venue_name':'Salesforce Park',
                 'date_phrase':'about a month from now'},
        'swag':{'quantity':24},
        'invitations':{'audience_phrase':'local founders'}}, status='ACTIVE')
with SessionLocal.begin() as session:
    for role, kind, output in (
        ('Events','event_research',{'venue_status':'UNCONFIRMED',
            'eventbrite_status':'NOT_CREATED','invitation_status':'DRAFT_ONLY'}),
        ('Buyer','product_source',{'publisher_status':'ok',
            'source_url':'https://www.printful.com/custom-water-bottles',
            'currency':'USD','unit_min':20.25,'unit_max':23.41,
            'subtotal_min':486.00,'subtotal_max':561.84,
            'checked_at':'2026-09-27T08:00:00+00:00','checkout_status':'NOT_READY'}),
        ('Treasurer','budget_review',{'review_status':'ESTIMATE_ONLY',
            'product_subtotal_min':486.00,'product_subtotal_max':561.84,
            'payment_status':'NONE','reserved_cents':0}),
    ):
        session.add(AgentJob(id=str(uuid4()), run_id=run_id, role=role, kind=kind,
                             input_json='{}', output_json=json.dumps(output), status='DONE'))
class FakeApp:
    def __init__(self, *, token):
        self.events = {}
    def event(self, name):
        def decorate(fn):
            self.events[name] = fn
            return fn
        return decorate
role_slack_bot.App = FakeApp
def forbidden_answer(*args, **kwargs):
    raise AssertionError('read-only recall must not call model, queue, or ledger')
role_slack_bot.answer = forbidden_answer
responses = []
def say(**options):
    responses.append(options)
    return {'ts':'900.' + str(len(responses)).zfill(6)}
with SessionLocal() as session:
    before_runs = session.scalar(select(func.count()).select_from(CrewRun))
    before_jobs = session.scalar(select(func.count()).select_from(AgentJob))
buyer = role_slack_bot.build_app('Buyer').events['app_mention']
buyer({'user':'U_A','channel':'C_DEMO','ts':'100.002','thread_ts':'100.001',
       'text':'<@Buyer> Live user-authored read-only check: what is the latest saved project in this thread, and what is the 24-bottle estimate? Please do not order.'},
      {'event_id':'buyer-recall'}, say)
assert '24 water bottles' in responses[-1]['text']
assert '$486.00–$561.84' in responses[-1]['text']
assert 'Checkout is not ready' in responses[-1]['text']
assert responses[-1]['thread_ts'] == '100.001'
treasurer = role_slack_bot.build_app('Treasurer').events['app_mention']
treasurer({'user':'U_A','channel':'C_DEMO','ts':'100.003','thread_ts':'100.001',
           'text':'<@Treasurer> What have we spent on the bottles for this project? Please distinguish the estimate from any payment.'},
          {'event_id':'treasurer-recall'}, say)
assert '$486.00–$561.84' in responses[-1]['text']
assert 'estimate review only' in responses[-1]['text']
assert 'no funds reserved or payment action' in responses[-1]['text']
assert responses[-1]['thread_ts'] == '100.001'
buyer({'user':'U_B','channel':'C_DEMO','ts':'100.004','thread_ts':'100.001',
       'text':'<@Buyer> What is the latest saved project in this thread?'},
      {'event_id':'other-user'}, say)
assert "don't see a saved project" in responses[-1]['text']
assert '$486.00' not in responses[-1]['text']
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == before_runs
    assert session.scalar(select(func.count()).select_from(AgentJob)) == before_jobs
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_concierge_negated_purchase_unconfirmed_recap_stays_read_only(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'concierge-recap.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_OWNER", "SLACK_ADMIN_USER_IDS": "",
        "SLACK_CONCIERGE_BOT_TOKEN": "test-concierge", "SLACK_APP_TOKEN": "test-app",
    })
    script = '''
import json
from uuid import uuid4
from sqlalchemy import func, select
from crew.db import AgentJob, CrewRun, SessionLocal, init_db
from crew.memory import create_or_update_project, is_project_recall_request
from crew import dialogue, slack_bot

init_db()
run_id = str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=run_id, flow='project-plan', source_user='U_OWNER',
                        channel_id='C_DEMO', status='COMPLETE'))
create_or_update_project(run_id=run_id, owner_user_id='U_OWNER', channel_id='C_DEMO',
    thread_root_ts='100.001', name='Salesforce Park gathering', safe_plan={
        'event':{'title':'SF gathering','venue_name':'Salesforce Park'},
        'swag':{'quantity':24}, 'invitations':{'audience_phrase':'local founders'}},
    status='ACTIVE')
with SessionLocal.begin() as session:
    for role, kind, output in (
        ('Events','event_research',{'venue_status':'UNCONFIRMED',
            'eventbrite_status':'NOT_CREATED','invitation_status':'DRAFT_ONLY'}),
        ('Buyer','product_source',{'publisher_status':'ok',
            'source_url':'https://www.printful.com/custom-water-bottles',
            'currency':'USD','unit_min':20.25,'unit_max':23.41,
            'subtotal_min':486.00,'subtotal_max':561.84,'checkout_status':'NOT_READY'}),
        ('Treasurer','budget_review',{'review_status':'ESTIMATE_ONLY',
            'payment_status':'NONE','reserved_cents':0}),
    ):
        session.add(AgentJob(id=str(uuid4()), run_id=run_id, role=role, kind=kind,
                             input_json='{}', output_json=json.dumps(output), status='DONE'))
class FakeApp:
    def __init__(self, *, token):
        self.events = {}
    def event(self, name):
        def decorate(fn):
            self.events[name] = fn
            return fn
        return decorate
    def command(self, name):
        return lambda fn: fn
slack_bot.App = FakeApp
def forbidden_answer(*args, **kwargs):
    raise AssertionError('recap must not call a model or queue work')
dialogue.answer = forbidden_answer
replies = []
def say(**options):
    replies.append(options)
    return {'ts':'900.001'}
request = ('Codex post-release guard test: do not buy any more water bottles for this plan. '
           'Please summarize what remains unconfirmed. Planning only; no checkout, payment, '
           'booking, publishing, or invitations.')
assert is_project_recall_request(request)
assert not dialogue._looks_like_purchase(request)
with SessionLocal() as session:
    before_runs = session.scalar(select(func.count()).select_from(CrewRun))
    before_jobs = session.scalar(select(func.count()).select_from(AgentJob))
mention = slack_bot.build_app().events['app_mention']
mention({'user':'U_OWNER','channel':'C_DEMO','ts':'100.002','thread_ts':'100.001',
         'text':'<@Concierge> ' + request}, {'event_id':'guard-recap'}, say)
reply = replies[-1]['text']
assert '24 water bottles' in reply and '$486.00–$561.84' in reply
assert 'Venue availability and booking are unconfirmed' in reply
assert 'Invitations: draft only' in reply
assert 'Checkout is not ready' in reply
assert 'no funds reserved or payment action' in reply
assert replies[-1]['thread_ts'] == '100.001'
with SessionLocal() as session:
    assert session.scalar(select(func.count()).select_from(CrewRun)) == before_runs
    assert session.scalar(select(func.count()).select_from(AgentJob)) == before_jobs
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
