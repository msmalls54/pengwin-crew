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
bot._submit_run = lambda flow, *, source_user, channel_id: queued.append(
    (flow, source_user, channel_id)) or 'run-123'
assert bot.is_slack_allowed('U_ALLOWED')
assert bot.is_slack_allowed('U_ADMIN')
assert not bot.is_slack_allowed('U_OTHER')
assert bot.mention_intent('We need oat milk') == 'berlin-pantry'
assert bot.mention_intent('A new hire starts') == 'welcome-kit'
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
assert 'run-123' in first, first
assert queued == [('berlin-pantry', 'U_ALLOWED', 'C_DEMO')]
assert 'already received' in bot.handle_command(command)
assert len(queued) == 1
assert 'EUR remaining' in bot.handle_command(
    {'user_id': 'U_ALLOWED', 'channel_id': 'C_DEMO', 'text': 'budgets'})
assert 'frozen' in bot.handle_command(
    {'user_id': 'U_ADMIN', 'channel_id': 'C_DEMO', 'text': 'freeze'})
assert 'FROZEN' in bot.handle_command(
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
