"""The public event proof is opt-in, scope-bound, and latest-revision-only."""

import os
import subprocess
import sys


def test_featured_project_scopes_private_facts_and_revisions(tmp_path):
    first_id = "11111111-1111-4111-8111-111111111111"
    second_id = "22222222-2222-4222-8222-222222222222"
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'featured.db'}",
        "PUBLIC_JUDGE_FEED": "true",
        "PUBLIC_DEMO_PROJECT_ID": first_id,
        "PUBLIC_DEMO_OWNER_USER_ID": "U_DEMO_OWNER",
        "SLACK_DEMO_CHANNEL_ID": "C_DEMO_CHANNEL",
        "SLACK_ALLOWED_USER_IDS": "U_DEMO_OWNER,U_OTHER",
    })
    script = r'''
import json
import os
from dataclasses import replace
from datetime import datetime, timezone
from uuid import uuid4
from fastapi.testclient import TestClient
from crew import app as app_module
from crew.db import AgentJob, CrewProject, CrewRun, SessionLocal
from crew.memory import create_or_update_project

first_id = "11111111-1111-4111-8111-111111111111"
second_id = "22222222-2222-4222-8222-222222222222"
owner = "U_DEMO_OWNER"
channel = "C_DEMO_CHANNEL"
secret = "private@example.com"

def plan(quantity):
    return {
        "kind": "project_proposal", "name": f"Private event for {secret}",
        "event": {"title": f"Private event for {secret}",
                  "venue_name": "Salesforce Park", "date_phrase": "in about a month"},
        "swag": {"product": "water_bottle", "quantity": quantity},
        "invitations": {"intent": "draft_invitations", "emails": [secret]},
    }

def add_run(run_id, quantity, status):
    with SessionLocal.begin() as session:
        session.add(CrewRun(id=run_id, flow="project-plan", source_user=owner,
                            channel_id=channel, status=status))
    project_id = create_or_update_project(run_id=run_id, owner_user_id=owner,
        channel_id=channel, thread_root_ts="123.456", name=f"Private event for {secret}",
        safe_plan=plan(quantity))
    assert project_id == first_id

def add_job(run_id, role, kind, output, status="DONE"):
    with SessionLocal.begin() as session:
        session.add(AgentJob(id=str(uuid4()), run_id=run_id, role=role,
            kind=kind, status=status, input_json=json.dumps({"private": secret}),
            output_json=json.dumps(output)))

def role_outputs(quantity):
    low, high = 20.25 * quantity, 23.41 * quantity
    return {
        "Events": {"venue_status": "UNCONFIRMED", "eventbrite_status": "NOT_CREATED",
                   "invitation_status": "DRAFT_ONLY", "date_options": ["2026-10-27", "2026-10-28"],
                   "invitation_draft": f"Invite {secret}",
                   "official_reservation_route": {
                       "operator": "Transbay Joint Powers Authority",
                       "url": "https://www.tjpa.org/permits-reservations", "availability": "UNCHECKED"}},
        "Buyer": {"status": "RESEARCHED", "product": "water_bottle",
                  "quantity": quantity, "publisher_status": "ok",
                  "price_kind": "PRODUCT_RANGE_ESTIMATE", "checkout_status": "NOT_READY",
                  "currency": "USD", "source_url": "https://www.printful.com/custom-water-bottles",
                  "checked_at": datetime.now(timezone.utc).isoformat(),
                  "unit_min": 20.25, "unit_max": 23.41,
                  "subtotal_min": round(low, 2), "subtotal_max": round(high, 2),
                  "private": secret},
        "Treasurer": {"review_status": "ESTIMATE_ONLY", "payment_status": "NONE",
                      "reserved_cents": 0, "currency": "USD",
                      "product_subtotal_min": round(low, 2),
                      "product_subtotal_max": round(high, 2),
                      "reason": f"Private event for {secret}"},
    }

with TestClient(app_module.app) as client:
    assert client.get("/api/public-activity").json()["featured_project"] is None
    add_run(first_id, 30, "COMPLETE")
    add_job(first_id, "Concierge", "dispatch", plan(30))
    first_outputs = role_outputs(30)
    add_job(first_id, "Events", "event_research", first_outputs["Events"])
    add_job(first_id, "Buyer", "product_source", first_outputs["Buyer"])
    add_job(first_id, "Treasurer", "budget_review", first_outputs["Treasurer"])
    featured = client.get("/api/public-activity").json()["featured_project"]
    assert featured["title"] == "Salesforce Park event"
    assert featured["revision_count"] == 1
    assert featured["latest_run_status"] == "COMPLETE"
    assert featured["buyer"]["quantity"] == 30
    assert featured["buyer"]["subtotal_min_cents"] == 60750
    assert featured["buyer"]["subtotal_max_cents"] == 70230
    assert featured["event"]["inquiry_url"] == "https://www.tjpa.org/permits-reservations"
    assert featured["treasury"]["payment_status"] == "none_in_project_review"
    assert {step["role"] for step in featured["steps"] if step["status"] == "completed"} == {
        "Concierge", "Events", "Buyer", "Treasurer"
    }
    serialized = json.dumps(featured)
    for private in (secret, owner, channel, first_id, "123.456", "Private event"):
        assert private not in serialized

    # An amendment may reuse the project, but its old quote and review cannot
    # become evidence for the new quantity while the current run is pending.
    add_run(second_id, 24, "RUNNING")
    add_job(second_id, "Concierge", "dispatch", plan(24))
    pending = client.get("/api/public-activity").json()["featured_project"]
    assert pending["revision_count"] == 2
    assert pending["latest_run_status"] == "RUNNING"
    assert pending["buyer"]["quantity"] == 24
    assert pending["buyer"]["subtotal_min_cents"] is None
    assert pending["event"]["inquiry_url"] is None
    assert pending["treasury"]["review_status"] == "pending"
    assert [step["status"] for step in pending["steps"]] == [
        "completed", "pending", "pending", "pending"]

    second_outputs = role_outputs(24)
    add_job(second_id, "Events", "event_research", second_outputs["Events"])
    add_job(second_id, "Buyer", "product_source", second_outputs["Buyer"])
    add_job(second_id, "Treasurer", "budget_review", second_outputs["Treasurer"])
    with SessionLocal.begin() as session:
        session.get(CrewRun, second_id).status = "COMPLETE"
    current = client.get("/api/public-activity").json()["featured_project"]
    assert current["buyer"]["quantity"] == 24
    assert current["buyer"]["subtotal_min_cents"] == 48600
    assert current["buyer"]["subtotal_max_cents"] == 56184
    assert current["treasury"]["review_status"] == "estimate_only"
    assert all(step["status"] == "completed" for step in current["steps"])

    # Any scope mismatch or changed venue removes the whole public snapshot.
    original_settings = app_module.settings
    for changes in (
        {"public_demo_project_id": ""},
        {"public_demo_project_id": second_id},
        {"public_demo_owner_user_id": "U_OTHER"},
    ):
        app_module.settings = replace(original_settings, **changes)
        assert client.get("/api/public-activity").json()["featured_project"] is None
    app_module.settings = original_settings
    os.environ["SLACK_DEMO_CHANNEL_ID"] = "C_OTHER"
    assert client.get("/api/public-activity").json()["featured_project"] is None
    os.environ["SLACK_DEMO_CHANNEL_ID"] = channel
    with SessionLocal.begin() as session:
        session.get(CrewRun, second_id).source_user = "U_OTHER"
    assert client.get("/api/public-activity").json()["featured_project"] is None
    with SessionLocal.begin() as session:
        session.get(CrewRun, second_id).source_user = owner
    with SessionLocal.begin() as session:
        project = session.get(CrewProject, first_id)
        changed = json.loads(project.plan_json)
        changed["event"]["venue_name"] = "Private home"
        project.plan_json = json.dumps(changed)
    assert client.get("/api/public-activity").json()["featured_project"] is None
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
