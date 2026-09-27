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
