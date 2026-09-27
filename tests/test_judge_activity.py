"""The judge token sees action proof, never the private office record."""

import os
import subprocess
import sys


def test_judge_feed_separates_outcomes_and_redacts_private_data(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'judge-feed.db'}",
        "ADMIN_TOKEN": "operator-token-more-than-24-characters",
        "WEB_DEMO_TOKEN": "judge-token-more-than-24-characters",
        "PUBLIC_JUDGE_FEED": "true",
        "PLANNER_MODE": "vultr",
        "SANDBOX_MODE": "docker",
        "PAYMENT_MODE": "simulated",
        "VULTR_MAX_CALLS": "40",
    })
    script = r'''
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from fastapi.testclient import TestClient
from crew.app import _judge_event, _judge_product_estimate, app
from crew.db import AgentJob, AuditEvent, ControlFlag, CrewRun, Payment, PendingOrder, Request, SessionLocal

with TestClient(app) as client:
    with SessionLocal.begin() as session:
        session.get(ControlFlag, "vultr_calls").value = "7"
        session.add(CrewRun(id="private-run", flow="eventbrite-event", source_user="U_PRIVATE_123",
                            channel_id="C_PRIVATE_123", status="WAITING_APPROVAL"))
        session.add(CrewRun(id="project-run", flow="project-plan", source_user="U_PRIVATE_123",
                            channel_id="C_PRIVATE_123", status="COMPLETE"))
        session.add(CrewRun(id="status-run", flow="event-status", source_user="U_PRIVATE_123",
                            channel_id="C_PRIVATE_123", status="COMPLETE"))
        session.flush()
        session.add(AgentJob(id="private-job", run_id="private-run", role="Events",
                             kind="eventbrite_publish", status="WAITING_APPROVAL",
                             input_json='{"email":"private@example.com"}'))
        source_output = {
            "status": "RESEARCHED", "product": "water_bottle", "quantity": 30,
            "publisher_status": "ok", "price_kind": "PRODUCT_RANGE_ESTIMATE",
            "checkout_status": "NOT_READY", "currency": "USD",
            "source_url": "https://www.printful.com/custom-water-bottles",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "unit_min": 20.25, "unit_max": 23.41,
            "subtotal_min": 607.5, "subtotal_max": 702.3,
            "email": "private@example.com", "private_text": "private Slack text",
        }
        session.add(AgentJob(id="source-job", run_id="project-run", role="Buyer",
                             kind="product_source", status="DONE", input_json='{"secret":"do-not-expose"}',
                             output_json=json.dumps(source_output)))
        session.add(AgentJob(id="research-job", run_id="project-run", role="Events",
                             kind="event_research", status="DONE", input_json='{}'))
        session.add(AgentJob(id="budget-job", run_id="project-run", role="Treasurer",
                             kind="budget_review", status="DONE", input_json='{}'))
        session.add(AgentJob(id="status-job", run_id="status-run", role="Events",
                             kind="eventbrite_status", status="DONE", input_json='{}'))
        session.add(AuditEvent(agent="Buyer", action="sandbox_code_attempt", request_id="private-run",
                               detail_json=json.dumps({"attempt": 1, "exit_code": 124,
                                                       "code_hash": "abc123abc123",
                                                       "secret": "do-not-expose"})))
        session.add(AuditEvent(agent="Concierge", action="run_queued", request_id="private-run",
                               detail_json=json.dumps({"source_user": "U_PRIVATE_123",
                                                       "text": "private Slack text"})))
        session.add(AuditEvent(agent="Treasurer", action="payment_submitted", request_id="private-run",
                               detail_json=json.dumps({"simulated": False, "provider_status": "PROCESSING",
                                                       "provider_ref": "private-provider-ref"})))
        session.add(AuditEvent(agent="Buyer", action="agent_job_done", request_id="project-run",
                               detail_json=json.dumps({"kind": "product_source", "private": "do-not-expose"})))
        for suffix, amount, currency, vendor, status, simulated in (
            ("sim", 1200, "USD", "bay-supply", "SIMULATED", True),
            ("sandbox", 1900, "EUR", "kaffee-kontor", "SUBMITTED_SANDBOX", False),
        ):
            session.add(Request(id=f"request-{suffix}", source="slack", source_user="U_PRIVATE_123",
                                text="private Slack text", office_id="SF" if currency == "USD" else "BER",
                                category="swag", sku="HOODIE-SF", requested_qty=1))
            session.flush()
            session.add(PendingOrder(id=f"order-{suffix}", request_id=f"request-{suffix}",
                                     vendor_id=vendor, sku="HOODIE-SF", qty=1,
                                     amount_cents=amount, currency=currency, status="PENDING"))
            session.flush()
            session.add(Payment(id=f"payment-{suffix}", pending_order_id=f"order-{suffix}",
                                request_id=f"request-{suffix}", amount_cents=amount,
                                currency=currency, method="simulated" if simulated else "airwallex_payout",
                                status=status, simulated=simulated, provider_ref="private-provider-ref"))

    demo = {"Authorization": "Bearer judge-token-more-than-24-characters"}
    admin = {"Authorization": "Bearer operator-token-more-than-24-characters"}
    assert client.get("/api/judge-activity").status_code == 401
    assert client.get("/api/judge-activity", headers={"Authorization": "Bearer invalid"}).status_code == 401
    assert client.get("/api/state", headers=demo).status_code == 401
    response = client.get("/api/judge-activity", headers=demo)
    assert response.status_code == 200, response.text
    public_response = client.get("/api/public-activity")
    assert public_response.status_code == 200, public_response.text
    assert public_response.json() == response.json() or (
        {k: v for k, v in public_response.json().items() if k != "as_of"}
        == {k: v for k, v in response.json().items() if k != "as_of"}
    )
    assert client.get("/api/judge-activity", headers=admin).status_code == 200
    body = response.json()
    runs = {run["flow"]: run for run in body["runs"]}
    assert runs["Free RSVP event"]["status"] == "WAITING_APPROVAL"
    assert runs["Free RSVP event"]["steps"][0]["status"] == "WAITING_APPROVAL"
    assert runs["Event and swag plan"]["status"] == "COMPLETE"
    assert {step["task"] for step in runs["Event and swag plan"]["steps"]} == {
        "Source water bottles", "Research event options", "Review estimated budget",
    }
    assert runs["Event status check"]["steps"][0]["task"] == "Check RSVP status"
    assert any(event["status"] == "held" and "exit 124" in event["evidence"]
               and "timeout status not stored" in event["evidence"] for event in body["activity"])
    assert any(event["status"] == "submitted" and "settlement unconfirmed" in event["evidence"] for event in body["activity"])
    assert any(event["title"] == "Source water bottles completed" for event in body["activity"])
    assert body["spend"]["proposed_mock_orders"] == [
        {"currency": "EUR", "amount_cents": 1900, "count": 1},
        {"currency": "USD", "amount_cents": 1200, "count": 1},
    ]
    assert body["spend"]["simulated_checkouts"] == [{"currency": "USD", "amount_cents": 1200, "count": 1}]
    assert body["spend"]["submitted_sandbox_transfers"] == [{"currency": "EUR", "amount_cents": 1900, "count": 1}]
    estimate = body["spend"]["water_bottle_product_estimate"]
    assert estimate["unit_min_cents"] == 2025 and estimate["unit_max_cents"] == 2341
    assert estimate["quantity"] == 30
    assert estimate["subtotal_min_cents"] == 60750 and estimate["subtotal_max_cents"] == 70230
    assert estimate["source_url"] == "https://www.printful.com/custom-water-bottles"
    assert body["spend"]["real_settled"] is None
    assert body["model_usage"]["attempted_calls"] == 7
    assert body["model_usage"]["call_limit"] == 40
    assert body["model_usage"]["billed_cost_usd"] is None
    # A private Slack run's timeout must never become a pinned public receipt.
    assert body["historical_containment"] is None
    serialized = json.dumps(body)
    for private in ("U_PRIVATE_123", "C_PRIVATE_123", "private Slack text", "private@example.com",
                    "private-provider-ref", "do-not-expose", "private-run", "private-job"):
        assert private not in serialized
    assert _judge_product_estimate(SimpleNamespace(role="Buyer", kind="product_source",
                                                    status="RUNNING", output_json=json.dumps(source_output))) is None
    invalid = {**source_output, "source_url": "https://private.example.com/collect"}
    assert _judge_product_estimate(SimpleNamespace(role="Buyer", kind="product_source",
                                                    status="DONE", output_json=json.dumps(invalid))) is None
    timeout_event = SimpleNamespace(agent="Buyer", action="sandbox_code_attempt",
        ts=datetime.now(timezone.utc), detail_json=json.dumps({
            "attempt": 1, "exit_code": 124, "code_hash": "abc123abc123",
            "timeout_reported": True,
        }))
    assert _judge_event(timeout_event)["status"] == "contained"
    timeout_event.detail_json = json.dumps({"attempt": 1, "exit_code": 124,
                                            "code_hash": "abc123abc123", "timeout_reported": False})
    assert _judge_event(timeout_event)["status"] == "failed"
    timeout_event.detail_json = json.dumps({"attempt": 1, "exit_code": 124,
                                            "code_hash": "abc123abc123"})
    assert _judge_event(timeout_event)["status"] == "held"
    invalid = {**source_output, "subtotal_max": 900.0}
    assert _judge_product_estimate(SimpleNamespace(role="Buyer", kind="product_source",
                                                    status="DONE", output_json=json.dumps(invalid))) is None
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_historical_web_timeout_survives_rolling_feed_and_stays_scoped(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'judge-history.db'}",
        "ADMIN_TOKEN": "operator-token-more-than-24-characters",
        "WEB_DEMO_TOKEN": "judge-token-more-than-24-characters",
        "PUBLIC_JUDGE_FEED": "true",
    })
    script = r'''
import hashlib
import json
from fastapi.testclient import TestClient
from crew.app import app
from crew.db import AgentJob, AuditEvent, CrewRun, SessionLocal

web_run = "12345678-1234-4234-8234-123456789abc"
private_run = "87654321-4321-4321-8321-cba987654321"
false_timeout_run = "abcdef12-3456-4456-8456-abcdef123456"
code = "print('Starting', flush=True)\nwhile True: pass"
code_hash = hashlib.sha256(code.encode()).hexdigest()[:12]
private_code = "while True: pass"
private_hash = hashlib.sha256(private_code.encode()).hexdigest()[:12]
false_code = "raise SystemExit(124)"
false_hash = hashlib.sha256(false_code.encode()).hexdigest()[:12]
with TestClient(app) as client:
    with SessionLocal.begin() as session:
        session.add(CrewRun(id=web_run, flow="code-task", source_user="web-demo",
                            channel_id="web", status="HELD"))
        session.add(CrewRun(id=private_run, flow="code-task", source_user="U_PRIVATE_123",
                            channel_id="C_PRIVATE_123", status="HELD"))
        session.add(CrewRun(id=false_timeout_run, flow="code-task", source_user="web-demo",
                            channel_id="web", status="HELD"))
        session.flush()
        session.add(AgentJob(id="web-job", run_id=web_run, role="Buyer", kind="code_execute",
            status="HELD", input_json='{}', output_json=json.dumps({"attempts": [{
                "code": code, "code_hash": code_hash, "exit_code": 124,
                "stderr": "Execution timed out after 10 seconds", "stdout": "",
            }]})))
        session.add(AgentJob(id="private-timeout-job", run_id=private_run, role="Buyer",
            kind="code_execute", status="HELD", input_json='{}', output_json=json.dumps({
                "attempts": [{"code": private_code, "code_hash": private_hash,
                              "exit_code": 124,
                              "stderr": "Execution timed out after 10 seconds", "stdout": ""}]})))
        session.add(AgentJob(id="false-timeout-job", run_id=false_timeout_run, role="Buyer",
            kind="code_execute", status="HELD", input_json='{}', output_json=json.dumps({
                "attempts": [{"code": false_code, "code_hash": false_hash,
                              "exit_code": 124, "stderr": "", "stdout": ""}]})))
        proof_event = AuditEvent(agent="Buyer", action="sandbox_code_attempt",
            request_id=web_run, detail_json=json.dumps({"attempt": 1, "exit_code": 124,
                "code_hash": code_hash, "private_text": "do-not-expose"}))
        session.add(proof_event)
        session.flush()
        proof_audit_id = proof_event.id
        session.add(AuditEvent(agent="Buyer", action="sandbox_code_attempt",
            request_id=private_run, detail_json=json.dumps({"attempt": 1, "exit_code": 124,
                "code_hash": private_hash, "private_text": "secret Slack request"})))
        session.add(AuditEvent(agent="Buyer", action="sandbox_code_attempt",
            request_id=web_run, detail_json=json.dumps({"attempt": 1, "exit_code": 124,
                "code_hash": "invalid", "private_text": "do-not-expose"})))
        session.add(AuditEvent(agent="Buyer", action="sandbox_code_attempt",
            request_id=false_timeout_run, detail_json=json.dumps({"attempt": 1,
                "exit_code": 124, "code_hash": false_hash})))
        # More than the API's 160-row scan and 36-event display window.
        for _ in range(180):
            session.add(AuditEvent(agent="Concierge", action="run_queued",
                request_id=private_run, detail_json='{"text":"secret Slack request"}'))

    response = client.get("/api/public-activity")
    assert response.status_code == 200
    body = response.json()
    assert len(body["activity"]) == 36
    assert not any(event["status"] == "contained" for event in body["activity"])
    proof = body["historical_containment"]
    assert proof == {
        "recorded_at": proof["recorded_at"], "run_id": web_run,
        "audit_event_id": proof_audit_id, "role": "Buyer",
        "status": "contained", "evidence": f"Attempt 1 · exit 124 · SHA-256 prefix {code_hash}",
        "source": "Saved Buyer sandbox audit and matching worker receipt for a web demo code run",
    }
    serialized = json.dumps(body)
    for private in (private_run, false_timeout_run, "U_PRIVATE_123", "C_PRIVATE_123",
                    "secret Slack request", "do-not-expose", private_hash, false_hash):
        assert private not in serialized
    assert client.get(f"/api/code-runs/{web_run}").status_code == 401
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_anonymous_activity_requires_explicit_demo_opt_in(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'judge-disabled.db'}",
        "ADMIN_TOKEN": "operator-token-more-than-24-characters",
        "WEB_DEMO_TOKEN": "judge-token-more-than-24-characters",
        "PUBLIC_JUDGE_FEED": "false",
    })
    script = r'''
from fastapi.testclient import TestClient
from crew.app import app

with TestClient(app) as client:
    assert client.get("/api/public-activity").status_code == 404
    assert client.get("/api/judge-activity").status_code == 401
    assert client.get("/api/judge-activity", headers={
        "Authorization": "Bearer judge-token-more-than-24-characters",
    }).status_code == 200
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
