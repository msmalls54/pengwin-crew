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
        "PLANNER_MODE": "vultr",
        "SANDBOX_MODE": "docker",
        "PAYMENT_MODE": "simulated",
        "VULTR_MAX_CALLS": "40",
    })
    script = r'''
import json
from fastapi.testclient import TestClient
from crew.app import app
from crew.db import AgentJob, AuditEvent, ControlFlag, CrewRun, Payment, PendingOrder, Request, SessionLocal

with TestClient(app) as client:
    with SessionLocal.begin() as session:
        session.get(ControlFlag, "vultr_calls").value = "7"
        session.add(CrewRun(id="private-run", flow="eventbrite-event", source_user="U_PRIVATE_123",
                            channel_id="C_PRIVATE_123", status="WAITING_APPROVAL"))
        session.flush()
        session.add(AgentJob(id="private-job", run_id="private-run", role="Events",
                             kind="eventbrite_publish", status="WAITING_APPROVAL",
                             input_json='{"email":"private@example.com"}'))
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
    assert client.get("/api/judge-activity", headers=admin).status_code == 200
    body = response.json()
    assert body["runs"][0]["status"] == "WAITING_APPROVAL"
    assert body["runs"][0]["steps"][0]["status"] == "WAITING_APPROVAL"
    assert any(event["status"] == "contained" and "exit 124" in event["evidence"] for event in body["activity"])
    assert any(event["status"] == "submitted" and "settlement unconfirmed" in event["evidence"] for event in body["activity"])
    assert body["spend"]["proposed_mock_orders"] == [
        {"currency": "EUR", "amount_cents": 1900, "count": 1},
        {"currency": "USD", "amount_cents": 1200, "count": 1},
    ]
    assert body["spend"]["simulated_checkouts"] == [{"currency": "USD", "amount_cents": 1200, "count": 1}]
    assert body["spend"]["submitted_sandbox_transfers"] == [{"currency": "EUR", "amount_cents": 1900, "count": 1}]
    assert body["spend"]["real_settled"] is None
    assert body["model_usage"]["attempted_calls"] == 7
    assert body["model_usage"]["call_limit"] == 40
    assert body["model_usage"]["billed_cost_usd"] is None
    serialized = json.dumps(body)
    for private in ("U_PRIVATE_123", "C_PRIVATE_123", "private Slack text", "private@example.com",
                    "private-provider-ref", "do-not-expose", "private-run", "private-job"):
        assert private not in serialized
'''
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
