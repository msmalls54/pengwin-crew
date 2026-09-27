"""Approval binds a reviewed event to one provider submission, without live calls."""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from crew.luma import LumaPlan, approval_preview, checked_luma_plan


def _future_plan(**changes):
    start = datetime.now(timezone.utc) + timedelta(days=3)
    data = dict(name="Pengwin lunch", start_at=start, end_at=start + timedelta(hours=1),
                timezone="Europe/Berlin", description="Lunch with the team",
                location="Berlin office", guests=["guest@example.com"])
    data.update(changes)
    return LumaPlan(**data)


def test_draft_preview_shows_every_provider_field_and_rejects_invented_guest():
    request = "Invite guest@example.com to a Pengwin lunch at the Berlin office."
    plan = checked_luma_plan(_future_plan(), request)
    preview = approval_preview("run-id", plan)
    assert all(value in preview for value in (
        "Pengwin lunch", "Lunch with the team", "Berlin office", "guest@example.com",
        plan.start_at.isoformat(), plan.end_at.isoformat(),
    ))
    with pytest.raises(ValueError, match="explicitly present"):
        checked_luma_plan(_future_plan(guests=["stranger@example.com"]), request)


def _run_script(tmp_path, name: str, script: str) -> None:
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / name}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "U_TEST",
        "LUMA_ENABLED": "true", "LUMA_API_KEY": "test-key",
    })
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_draft_waits_for_exact_slack_approval(tmp_path):
    _run_script(tmp_path, "luma-approved.db", """
import os
from datetime import datetime, timedelta, timezone
from crew.db import AgentJob, SessionLocal
from crew.seed import seed_demo
import crew.jobs as jobs
from crew.luma import LumaPlan, plan_snapshot
from sqlalchemy import select

seed_demo(reset=True)
start = datetime.now(timezone.utc) + timedelta(days=3)
plan = LumaPlan(name='Pengwin lunch', start_at=start,
                end_at=start + timedelta(hours=1), timezone='Europe/Berlin',
                description='Lunch with the team', location='Berlin office',
                guests=['guest@example.com'])
class FakeInference:
    def luma_event_plan(self, *, request_text):
        return plan
jobs.VultrInference = FakeInference
posts = []
jobs.post_role_update = lambda role, channel, message, thread_ts=None: posts.append(message) or '123.45'
run_id = jobs.submit_run('luma-event', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Invite guest@example.com to a Pengwin lunch at the Berlin office.')
assert jobs.run_one_job('Concierge')
assert jobs.get_run(run_id)['status'] == 'WAITING_APPROVAL'
assert not jobs.run_one_job('Events')
assert 'guest@example.com' in jobs.review_luma_run(run_id, user_id='U_TEST')
snapshot = plan_snapshot(plan)
assert 'does not match' in jobs.approve_luma_run(run_id, '000000000000', user_id='U_TEST')
assert 'Only a configured' in jobs.approve_luma_run(run_id, snapshot, user_id='U_OTHER')
assert 'Approved' in jobs.approve_luma_run(run_id, snapshot, user_id='U_TEST')
assert 'No event draft' in jobs.approve_luma_run(run_id, snapshot, user_id='U_TEST')
calls = []
class FakeLuma:
    def create_event(self, plan):
        calls.append('create')
        return 'evt-test123'
    def send_invites(self, event_id, guests):
        calls.append(('invite', event_id, tuple(guests)))
        return []
jobs.LumaClient = FakeLuma
assert jobs.run_one_job('Events')
assert not jobs.run_one_job('Events')
assert calls == ['create', ('invite', 'evt-test123', ('guest@example.com',))]
assert jobs.get_run(run_id)['status'] == 'COMPLETE'
""")


def test_luma_provider_uncertainty_holds_run_without_retry(tmp_path):
    _run_script(tmp_path, "luma-held.db", """
from datetime import datetime, timedelta, timezone
from crew.seed import seed_demo
import crew.jobs as jobs
from crew.luma import LumaPlan, plan_snapshot

seed_demo(reset=True)
start = datetime.now(timezone.utc) + timedelta(days=3)
plan = LumaPlan(name='Pengwin lunch', start_at=start,
                end_at=start + timedelta(hours=1), timezone='Europe/Berlin',
                location='Berlin office', guests=['guest@example.com'])
class FakeInference:
    def luma_event_plan(self, *, request_text):
        return plan
jobs.VultrInference = FakeInference
jobs.post_role_update = lambda *args, **kwargs: '123.45'
run_id = jobs.submit_run('luma-event', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Invite guest@example.com to a Pengwin lunch at the Berlin office.')
assert jobs.run_one_job('Concierge')
assert 'Approved' in jobs.approve_luma_run(run_id, plan_snapshot(plan), user_id='U_TEST')
class FakeLuma:
    def create_event(self, plan):
        return 'evt-created'
    def send_invites(self, event_id, guests):
        raise TimeoutError('outcome unknown')
jobs.LumaClient = FakeLuma
assert jobs.run_one_job('Events')
run = jobs.get_run(run_id)
assert run['status'] == 'HELD', run
assert run['jobs'][-1]['status'] == 'HELD'
assert run['jobs'][-1]['output']['event_id'] == 'evt-created'
assert not jobs.run_one_job('Events')
""")
