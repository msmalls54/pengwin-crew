"""Read-only venue and budget follow-ups use saved, scoped project facts."""

import os
import subprocess
import sys


def test_replacement_research_and_reallocation_are_scoped_and_read_only(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'adaptive.db'}",
        "PAYMENT_MODE": "simulated",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST,U_OTHER",
        "BRAVE_SEARCH_API_KEY": "",
    })
    script = r'''
from datetime import datetime, timezone
from sqlalchemy import select
from crew.db import AgentJob, Budget, Payment, PendingOrder, SessionLocal
from crew.project_planner import EventProposal, ProjectPlan, SwagProposal
from crew.research import PublisherFact, ResearchResult, SearchLead
from crew.seed import seed_demo
import crew.jobs as jobs
import crew.research as research

seed_demo(reset=True)
with SessionLocal.begin() as session:
    swag = session.execute(select(Budget).where(Budget.office_id == 'SF', Budget.category == 'swag')).scalar_one()
    swag.limit_cents = 50000

class FakeInference:
    def project_plan(self, *, request_text, context=''):
        return ProjectPlan(name='Park meetup', event=EventProposal(
            title='Park meetup', venue_name='Salesforce Park', date_phrase='next month'),
            swag=SwagProposal(quantity=24))

jobs.VultrInference = FakeInference
jobs._notify = lambda *args: None
research.lookup_project_facts = lambda query, *, kind='web', location=None, max_results=3: ResearchResult(
    status='unavailable', kind=kind, query=query, reason='No Brave key')
research.check_printful_water_bottle_prices = lambda: PublisherFact(
    status='ok', source_url='https://www.printful.com/custom-water-bottles',
    checked_at=datetime.now(timezone.utc), currency='USD', min_price=20.25, max_price=23.41)

run_id = jobs.submit_run('project-plan', source_user='U_TEST', channel_id='C_DEMO',
    request_text='Plan an event at Salesforce Park next month with 24 water bottles.')
for role in ('Concierge', 'Events', 'Buyer', 'Treasurer'):
    assert jobs.run_one_job(role), role
assert jobs.get_run(run_id)['status'] == 'COMPLETE'

def alternatives(query, *, kind='web', location=None, max_results=3):
    assert (query, kind, location, max_results) == (
        'parks for events', 'place', 'san francisco ca united states', 5)
    return ResearchResult(status='ok', kind='place', query=query, location=location,
        searched_at=datetime.now(timezone.utc), results=[
            SearchLead(title='Salesforce Park', url='https://example.com/original'),
            SearchLead(title='Bay Conference Center', url='https://example.com/bay'),
            SearchLead(title='Mission Event Hall', url='https://example.com/mission'),
        ])
research.lookup_project_facts = alternatives
with SessionLocal() as session:
    budgets_before = [(b.office_id, b.category, b.limit_cents, b.spent_cents, b.reserved_cents)
                      for b in session.execute(select(Budget)).scalars().all()]
    jobs_before = session.execute(select(AgentJob)).scalars().all()

venue = jobs.research_venue_alternatives(user_id='U_TEST', channel_id='C_DEMO')
assert 'Bay Conference Center' in venue and 'Mission Event Hall' in venue
assert 'example.com/original' not in venue
assert 'not availability confirmations' in venue
budget = jobs.suggest_budget_reallocation(user_id='U_TEST', channel_id='C_DEMO')
assert '$61.84' in budget and 'office-supplies' in budget
assert 'No funds were moved' in budget
assert "don't see a venue" in jobs.research_venue_alternatives(
    user_id='U_OTHER', channel_id='C_DEMO')
assert "don't see a venue" in jobs.research_venue_alternatives(
    user_id='U_TEST', channel_id='C_DEMO', thread_root_ts='123.456')
assert 'need a current itemized' in jobs.suggest_budget_reallocation(
    user_id='U_OTHER', channel_id='C_DEMO')
with SessionLocal() as session:
    budgets_after = [(b.office_id, b.category, b.limit_cents, b.spent_cents, b.reserved_cents)
                     for b in session.execute(select(Budget)).scalars().all()]
    assert budgets_after == budgets_before
    assert len(session.execute(select(AgentJob)).scalars().all()) == len(jobs_before)
    assert session.execute(select(Payment)).scalars().all() == []
    assert session.execute(select(PendingOrder)).scalars().all() == []

plan = ProjectPlan(name='Park meetup', event=EventProposal(
    title='Park meetup', venue_name='Salesforce Park'))
output, message = jobs._project_event_work(None, plan.model_dump(mode='json'),
    'Salesforce Park is unavailable; find alternatives')
assert output['venue_status'] == 'REPORTED_UNAVAILABLE'
assert '[replacement venue]' in output['description_draft']
assert 'Salesforce Park' not in output['description_draft']
assert 'Bay Conference Center' in message
assert not jobs._venue_reported_unavailable('Salesforce Park is available', 'Salesforce Park')
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
