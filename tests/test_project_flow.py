"""The proposed event flow is durable and never treats research as a purchase."""

import os
import subprocess
import sys


def test_event_bottles_invitations_coordinate_without_provider_writes(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'project.db'}",
        "PAYMENT_MODE": "simulated",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST,U_OTHER",
        "SLACK_ADMIN_USER_IDS": "",
        "BRAVE_SEARCH_API_KEY": "",
    })
    script = r'''
from datetime import datetime, timezone
from sqlalchemy import select
from crew.db import (AgentJob, CrewProject, Payment, PendingOrder,
                     RunResourceLink, SessionLocal)
from crew.project_planner import (EventProposal, InvitationProposal,
                                  ProjectPlan, SwagProposal)
from crew.research import PublisherFact, ResearchResult
from crew.seed import seed_demo
import crew.jobs as jobs
import crew.research as research

request = ("Plan a Pengwin event at Golden Gate Park in about a month, "
           "get 3 water bottles, and draft invitations for local startup founders.")
class FakeInference:
    def project_plan(self, *, request_text, context=""):
        assert request_text == request
        return ProjectPlan(
            name="Pengwin event",
            event=EventProposal(title="Pengwin event", date_phrase="in about a month",
                                venue_name="Golden Gate Park"),
            swag=SwagProposal(quantity=3),
            invitations=InvitationProposal(audience_phrase="local startup founders"),
        )

def fake_search(query, *, kind="web", max_results=3):
    return ResearchResult(status="unavailable", kind=kind, query=query,
                          reason="No Brave key")

def fake_printful():
    return PublisherFact(status="ok", source_url="https://www.printful.com/custom-water-bottles",
                         checked_at=datetime.now(timezone.utc), currency="USD",
                         min_price=20.25, max_price=23.41)

seed_demo(reset=True)
jobs.VultrInference = FakeInference
research.lookup_project_facts = fake_search
research.check_printful_water_bottle_prices = fake_printful
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
run_id = jobs.submit_run("project-plan", source_user="U_TEST", channel_id="C_DEMO",
                         request_text=request)
assert jobs.run_one_job("Concierge")
assert jobs.run_one_job("Events")
assert jobs.run_one_job("Buyer")
assert jobs.run_one_job("Treasurer")
run = jobs.get_run(run_id)
assert run["status"] == "COMPLETE", run
assert {entry["role"] for entry in run["jobs"]} == {"Concierge", "Events", "Buyer", "Treasurer"}
with SessionLocal() as session:
    assert session.get(CrewProject, run_id) is not None
    assert session.execute(select(PendingOrder)).scalars().all() == []
    assert session.execute(select(Payment)).scalars().all() == []
    links = session.execute(select(RunResourceLink).where(RunResourceLink.run_id == run_id)).scalars().all()
    assert {link.resource_kind for link in links} == {"publisher_product", "budget_review"}
assert any("has not reserved a venue" in text for role, text in messages if role == "Events")
assert any("$60.75–$70.23" in text for role, text in messages if role == "Buyer")
assert any("No funds were reserved" in text for role, text in messages if role == "Treasurer")
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr


def test_event_status_readback_and_failure_are_distinct(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'events.db'}",
        "PAYMENT_MODE": "simulated",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO",
        "SLACK_ALLOWED_USER_IDS": "U_TEST,U_OTHER",
    })
    script = r'''
import json
from datetime import datetime, timezone
from uuid import uuid4
from crew.db import AgentJob, CrewRun, SessionLocal
from crew.luma import LumaPlan
from crew.seed import seed_demo
import crew.jobs as jobs

seed_demo(reset=True)
plan = LumaPlan(name="Pengwin demo", start_at=datetime(2026, 10, 2, 17, tzinfo=timezone.utc),
                end_at=datetime(2026, 10, 2, 18, tzinfo=timezone.utc),
                timezone="America/Los_Angeles", meeting_url="https://example.com/room",
                capacity=40)
published_id = str(uuid4())
with SessionLocal.begin() as session:
    session.add(CrewRun(id=published_id, flow="eventbrite-event", source_user="U_TEST",
                        channel_id="C_DEMO", status="COMPLETE"))
    session.flush()
    session.add(AgentJob(id=str(uuid4()), run_id=published_id, role="Events",
                         kind="eventbrite_publish", status="DONE",
                         input_json=json.dumps({"plan": plan.model_dump(mode="json")}),
                         output_json=json.dumps({"event_id": "12345", "url":
                                                 "https://www.eventbrite.com/e/pengwin-demo-tickets-12345"})))
messages = []
jobs._notify = lambda run_id, role, message: messages.append((role, message))
class LiveClient:
    def read_status(self, event_id):
        assert event_id == "12345"
        return {"status": "live", "url": "https://www.eventbrite.com/e/pengwin-demo-tickets-12345",
                "listed": False}
jobs.EventbriteClient = LiveClient
lookup = jobs.submit_run("event-status", source_user="U_TEST", channel_id="C_DEMO",
                         request_text="on Oct 2nd")
assert jobs.run_one_job("Concierge")
assert jobs.run_one_job("Events")
assert jobs.get_run(lookup)["status"] == "COMPLETE"
assert any("live now (checked" in text and "(unlisted)" in text for role, text in messages if role == "Events")
class DownClient:
    def read_status(self, event_id):
        raise RuntimeError("provider unavailable")
jobs.EventbriteClient = DownClient
lookup = jobs.submit_run("event-status", source_user="U_TEST", channel_id="C_DEMO",
                         request_text="on Oct 2nd")
assert jobs.run_one_job("Concierge")
assert jobs.run_one_job("Events")
assert jobs.get_run(lookup)["status"] == "COMPLETE"
assert any("current status not checked" in text for role, text in messages if role == "Events")
other = jobs.submit_run("event-status", source_user="U_OTHER", channel_id="C_DEMO",
                        request_text="on Oct 2nd")
assert jobs.run_one_job("Concierge")
assert jobs.run_one_job("Events")
assert jobs.get_run(other)["status"] == "COMPLETE"
assert "don't see a saved Pengwin" in messages[-1][1]
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
