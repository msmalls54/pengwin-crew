"""A public registration page requires an exact Slack approval and one submission."""

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from crew.eventbrite import checked_eventbrite_plan
from crew.luma import LumaPlan


def test_vultr_event_planner_accepts_observed_optional_field_format():
    script = '''
from crew import inference
observed = {}
class Response:
    def raise_for_status(self): pass
    def json(self):
        return {"choices": [{"message": {"content": (
            '{"name":"Pengwin review","start_at":"2026-10-02T14:00:00-07:00",'
            '"end_at":"2026-10-02T15:00:00-07:00",'
            '"timezone":"America/Los_Angeles","description":null,"location":"Online",'
            '"meeting_url":"https://example.com/room","guests":null,"capacity":25,'
            '"clarification":null}'
        )}}]}
def fake_post(*args, **kwargs):
    observed.update(kwargs["json"])
    return Response()
inference.reserve_inference_call = lambda: None
inference.httpx.post = fake_post
plan = inference.VultrInference(key="test", model="deepseek-v4.1-flash").luma_event_plan(
    request_text="Pengwin review for 25 people at https://example.com/room")
assert observed["max_tokens"] >= 2500
assert plan.description == "" and plan.guests == []
assert plan.location is None and plan.meeting_url == "https://example.com/room"
'''
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr


def _plan(**changes):
    start = datetime.now(timezone.utc) + timedelta(days=7)
    values = dict(name="Pengwin gathering", start_at=start,
                  end_at=start + timedelta(hours=1), timezone="America/Los_Angeles",
                  description="Pengwin demo", meeting_url="https://example.com/room",
                  capacity=25)
    values.update(changes)
    return LumaPlan(**values)


def test_public_rsvp_requires_stated_capacity_and_no_unsent_named_guests():
    request = "Create Pengwin gathering for 25 people at https://example.com/room next week."
    assert checked_eventbrite_plan(_plan(), request).capacity == 25
    with pytest.raises(ValueError, match="capacity"):
        checked_eventbrite_plan(_plan(capacity=26), request)
    with pytest.raises(ValueError, match="does not email named guests"):
        checked_eventbrite_plan(_plan(guests=["guest@example.com"]), request + " Invite guest@example.com")


def test_eventbrite_publishing_waits_for_exact_slack_approval(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'eventbrite.db'}",
        "PAYMENT_MODE": "simulated", "PLANNER_MODE": "deterministic",
        "SANDBOX_MODE": "local", "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST", "SLACK_ADMIN_USER_IDS": "U_TEST",
        "EVENTBRITE_ENABLED": "true",
    })
    script = """
from datetime import datetime, timedelta, timezone
from crew.seed import seed_demo
import crew.jobs as jobs
from crew.luma import LumaPlan, plan_snapshot

seed_demo(reset=True)
start = datetime.now(timezone.utc) + timedelta(days=7)
plan = LumaPlan(name='Pengwin gathering', start_at=start,
                end_at=start + timedelta(hours=1), timezone='America/Los_Angeles',
                description='Pengwin demo', meeting_url='https://example.com/room', capacity=25)
class FakeInference:
    def luma_event_plan(self, *, request_text): return plan
jobs.VultrInference = FakeInference
jobs.post_role_update = lambda *args, **kwargs: '123.45'
run_id = jobs.submit_run('eventbrite-event', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Create Pengwin gathering for 25 people at https://example.com/room next week.')
assert jobs.run_one_job('Concierge')
assert jobs.get_run(run_id)['status'] == 'WAITING_APPROVAL'
assert not jobs.run_one_job('Events')
snapshot = plan_snapshot(plan)
assert 'does not match' in jobs.approve_luma_run(run_id, '000000000000', user_id='U_TEST')
assert 'Approved' in jobs.approve_luma_run(run_id, snapshot, user_id='U_TEST')
calls = []
class FakeEventbrite:
    def create_draft(self, plan):
        calls.append('draft'); return '12345'
    def create_free_ticket(self, event_id, capacity):
        calls.append(('ticket', event_id, capacity)); return '67890'
    def publish(self, event_id):
        calls.append(('publish', event_id)); return 'https://www.eventbrite.com/e/test-tickets-12345'
jobs.EventbriteClient = FakeEventbrite
assert jobs.run_one_job('Events')
assert not jobs.run_one_job('Events')
assert calls == ['draft', ('ticket', '12345', 25), ('publish', '12345')]
assert jobs.get_run(run_id)['status'] == 'COMPLETE'
assert jobs.get_run(run_id)['jobs'][-1]['output']['url'].startswith('https://www.eventbrite.com/')

# If publishing times out after a provider-side mutation, keep the known IDs
# and hold the run. Never submit the event again on a worker retry.
class PublishTimeout(FakeEventbrite):
    def publish(self, event_id):
        calls.append(('publish-timeout', event_id))
        raise TimeoutError('ambiguous publish outcome')
jobs.EventbriteClient = PublishTimeout
second = jobs.submit_run('eventbrite-event', source_user='U_TEST', channel_id='C_DEMO',
                         request_text='Create Pengwin gathering for 25 people at https://example.com/room next week.')
assert jobs.run_one_job('Concierge')
assert 'Approved' in jobs.approve_luma_run(second, snapshot, user_id='U_TEST')
assert jobs.run_one_job('Events')
held = jobs.get_run(second)
assert held['status'] == 'HELD'
assert held['jobs'][-1]['output']['event_id'] == '12345'
assert held['jobs'][-1]['output']['ticket_id'] == '67890'
assert held['jobs'][-1]['error'] == 'TimeoutError'
assert not jobs.run_one_job('Events')
assert calls.count(('publish-timeout', '12345')) == 1
"""
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
