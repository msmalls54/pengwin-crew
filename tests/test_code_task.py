"""The code task records executed output and repairs one failing sandbox attempt."""

import json
import os
import subprocess
import sys


def test_worker_returns_stderr_for_repair_without_leaking_a_traceback():
    env = os.environ.copy()
    env.pop("MOCK_STORE_URL", None)
    env["TASK_JSON"] = json.dumps({"action": "execute_code",
                                   "code": "raise ValueError('repair me')", "inputs": {}})
    result = subprocess.run([sys.executable, "sandbox/worker.py"], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["exit_code"] != 0
    assert "repair me" in payload["stderr"]


def test_worker_program_exit_124_is_not_a_timeout_response():
    env = os.environ.copy()
    env.pop("MOCK_STORE_URL", None)
    env["TASK_JSON"] = json.dumps({"action": "execute_code",
                                   "code": "raise SystemExit(124)", "inputs": {}})
    result = subprocess.run([sys.executable, "sandbox/worker.py"], env=env,
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["exit_code"] == 124
    assert payload["stderr"] == ""


def test_plain_english_code_task_retries_error_then_reports_executed_result(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'code.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "docker", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "U_TEST",
    })
    script = """
from crew.inference import CodeDraft
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
class FakeInference:
    def code_draft(self, *, goal, previous_code='', stderr=''):
        assert goal == 'Calculate 19 plus 23'
        if previous_code:
            assert 'SyntaxError' in stderr
            return CodeDraft(code='print(19+23)')
        return CodeDraft(code='bad syntax')
jobs.VultrInference = FakeInference
calls = []
class FakeSandbox:
    def execute_code(self, code, inputs):
        calls.append((code, inputs))
        if len(calls) == 1:
            return {'exit_code': 1, 'stdout': '', 'stderr': 'SyntaxError: invalid syntax'}
        return {'exit_code': 0, 'stdout': '42\\n', 'stderr': ''}
jobs.sandbox = lambda: FakeSandbox()
run_id = jobs.submit_run('code-task', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Calculate 19 plus 23')
assert jobs.run_one_job('Concierge')
assert jobs.run_one_job('Buyer')
run = jobs.get_run(run_id)
assert run['status'] == 'COMPLETE', run
assert run['jobs'][-1]['output']['result'] == '42'
assert [attempt['exit_code'] for attempt in run['jobs'][-1]['output']['attempts']] == [1, 0]
assert len(calls) == 2
assert all(value['goal'] == 'Calculate 19 plus 23' for _, value in calls)
assert any('It printed:\\n42' in message for _, message in messages)
assert not jobs.run_one_job('Buyer')
"""
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_timeout_is_visible_and_is_not_retried(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'timeout.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "docker", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "U_TEST",
    })
    script = """
from crew.inference import CodeDraft
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
class FakeInference:
    def code_draft(self, *, goal, previous_code='', stderr=''):
        assert not previous_code, 'Timeout must not trigger a repair attempt'
        return CodeDraft(code='while True: pass')
jobs.VultrInference = FakeInference
calls = []
class FakeSandbox:
    def execute_code(self, code, inputs):
        calls.append(code)
        return {'exit_code': 124, 'stdout': '', 'stderr': 'Execution timed out after 10 seconds'}
jobs.sandbox = lambda: FakeSandbox()
run_id = jobs.submit_run('code-task', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Run an infinite loop')
assert jobs.run_one_job('Concierge')
assert jobs.run_one_job('Buyer')
run = jobs.get_run(run_id)
assert run['status'] == 'HELD', run
assert len(calls) == 1
assert run['jobs'][-1]['output']['attempts'][0]['exit_code'] == 124
assert 'timed out' in run['jobs'][-1]['output']['attempts'][0]['stderr']
assert any('stopped that code after 10 seconds' in message for _, message in messages)
"""
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_program_exit_124_is_repaired_without_a_timeout_claim(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'program-exit-124.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "docker", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "U_TEST",
    })
    script = r'''
import json
from sqlalchemy import select
from crew.db import AuditEvent, SessionLocal
from crew.inference import CodeDraft
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
class FakeInference:
    def code_draft(self, *, goal, previous_code='', stderr=''):
        if previous_code:
            assert previous_code == 'raise SystemExit(124)'
            assert stderr == 'Process exited with code 124.'
            return CodeDraft(code='print(42)')
        return CodeDraft(code='raise SystemExit(124)')
jobs.VultrInference = FakeInference
calls = []
class FakeSandbox:
    def execute_code(self, code, inputs):
        calls.append(code)
        if len(calls) == 1:
            return {'exit_code': 124, 'stdout': '', 'stderr': ''}
        return {'exit_code': 0, 'stdout': '42\n', 'stderr': ''}
jobs.sandbox = lambda: FakeSandbox()
run_id = jobs.submit_run('code-task', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Calculate 42')
assert jobs.run_one_job('Concierge')
assert jobs.run_one_job('Buyer')
run = jobs.get_run(run_id)
assert run['status'] == 'COMPLETE', run
assert [item['exit_code'] for item in run['jobs'][-1]['output']['attempts']] == [124, 0]
assert run['jobs'][-1]['output']['attempts'][0]['timeout_reported'] is False
assert run['jobs'][-1]['output']['result'] == '42'
assert not any('10 seconds' in message for _, message in messages)
with SessionLocal() as session:
    detail_json = session.execute(select(AuditEvent.detail_json).where(
        AuditEvent.action == 'sandbox_code_attempt').order_by(AuditEvent.id)).scalars().first()
assert json.loads(detail_json)['timeout_reported'] is False
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
