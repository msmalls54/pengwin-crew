"""Exercise Slack command routing without connecting to a real workspace."""

import os
import subprocess
import sys


def test_slack_allowlist_commands_and_retry_guard(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'slack-test.db'}",
        "PAYMENT_MODE": "simulated",
        "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local",
        "SLACK_ALLOWED_USER_IDS": "U_ALLOWED",
        "SLACK_ADMIN_USER_IDS": "U_ADMIN",
    })
    script = """
from crew.seed import seed_demo
from crew.slack_bot import handle_command, is_slack_allowed, mention_intent
from crew.db import Payment, SessionLocal
from sqlalchemy import select, func

seed_demo(reset=True)
assert is_slack_allowed('U_ALLOWED')
assert is_slack_allowed('U_ADMIN')
assert not is_slack_allowed('U_OTHER')
assert mention_intent('We need oat milk') == 'berlin-pantry'
assert mention_intent('A new hire starts') == 'welcome-kit'
assert 'not allowed' in handle_command({'user_id': 'U_OTHER', 'text': 'pantry', 'trigger_id': 't0'})
assert 'not allowed' in handle_command({'user_id': 'U_OTHER', 'text': 'budgets'})
assert 'admin' in handle_command({'user_id': 'U_ALLOWED', 'text': 'freeze'})
assert 'no delivery ID' in handle_command({'user_id': 'U_ALLOWED', 'text': 'pantry'})

command = {'user_id': 'U_ALLOWED', 'text': 'pantry', 'trigger_id': 't1'}
first = handle_command(command)
assert 'SIMULATED' in first, first
with SessionLocal() as session:
    before = session.execute(select(func.count()).select_from(Payment)).scalar_one()
assert before == 2
assert 'already received' in handle_command(command)
with SessionLocal() as session:
    after = session.execute(select(func.count()).select_from(Payment)).scalar_one()
assert after == before
assert 'EUR remaining' in handle_command({'user_id': 'U_ALLOWED', 'text': 'budgets'})
assert 'frozen' in handle_command({'user_id': 'U_ADMIN', 'text': 'freeze'})
assert 'FROZEN' in handle_command({'user_id': 'U_ALLOWED', 'text': 'status'})
assert 'unfrozen' in handle_command({'user_id': 'U_ADMIN', 'text': 'unfreeze'})
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
