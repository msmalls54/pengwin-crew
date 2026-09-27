import os


def test_local_purchase_attack_and_freeze(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'crew-test.db'}")
    monkeypatch.setenv("ADMIN_TOKEN", "local-test-token-with-more-than-24-characters")
    monkeypatch.setenv("PAYMENT_MODE", "simulated")
    monkeypatch.setenv("PLANNER_MODE", "deterministic")
    monkeypatch.setenv("SANDBOX_MODE", "local")

    from crew.seed import seed_demo
    from crew.workflow import run_flow
    from crew.payments import pay_pending_order
    from crew.db import Budget, ControlFlag, Payment, SessionLocal, Task
    from sqlalchemy import select

    seed_demo(reset=True)
    pantry = run_flow("berlin-pantry")
    assert [item["first_status"] for item in pantry] == ["SIMULATED", "SIMULATED"]
    attack_results = run_flow("hoodie-attack")
    attack = attack_results[0]
    assert attack["first_status"] == "BLOCKED"
    assert attack["blocked_rule"] == "QUANTITY_SANITY"
    assert attack["corrected_status"] == "SIMULATED"
    assert attack_results[1]["first_status"] == "SIMULATED"
    assert pay_pending_order(attack["first_order_id"]).id == attack["first_payment_id"]
    new_hire = run_flow("welcome-kit")
    assert [part["first_status"] for part in new_hire] == ["SIMULATED", "SIMULATED"]
    assert new_hire[1]["invite_ready"] is True

    with SessionLocal.begin() as session:
        swag = session.execute(select(Budget).where(Budget.office_id == "BER", Budget.category == "swag")).scalar_one()
        assert swag.spent_cents == 20 * 3200
        assert swag.reserved_cents == 0
        assert session.execute(select(Payment).where(Payment.pending_order_id == attack["first_order_id"])).scalar_one().provider_ref is None
        import json
        event_task = session.execute(select(Task).where(Task.request_id == new_hire[1]["request_id"], Task.agent == "Events")).scalar_one()
        invite = json.loads(event_task.output_json)["ics"]
        assert "BEGIN:VCALENDAR" in invite
        assert "SUMMARY:Berlin new-hire team lunch" in invite
        session.get(ControlFlag, "freeze").value = "true"

    frozen = run_flow("welcome-kit")[0]
    assert frozen["first_status"] == "BLOCKED"
    assert frozen["blocked_rule"] == "FREEZE"


def test_web_mutations_require_admin_token(tmp_path, monkeypatch):
    # Separate process keeps the Settings singleton isolated from other tests.
    import subprocess
    import sys
    env = os.environ.copy()
    env.update({"DATABASE_URL": f"sqlite:///{tmp_path / 'api-test.db'}",
                "ADMIN_TOKEN": "local-test-token-with-more-than-24-characters",
                "WEB_DEMO_TOKEN": "separate-demo-token-with-more-than-24-characters",
                "PLANNER_MODE": "vultr", "SANDBOX_MODE": "docker"})
    script = """
from fastapi.testclient import TestClient
from crew.app import app
import crew.workflow as workflow
from crew.sandbox import LocalCatalogSandbox
from crew.inference import BuyerChoice
workflow.sandbox=lambda:LocalCatalogSandbox()
class FakeInference:
    def buyer_choice(self, *, requested_sku, requested_qty, page_text):
        return BuyerChoice(sku=requested_sku, quantity=requested_qty, reason='test')
workflow.VultrInference=FakeInference
with TestClient(app) as client:
    headers={'Authorization': 'Bearer local-test-token-with-more-than-24-characters'}
    demo_headers={'Authorization': 'Bearer separate-demo-token-with-more-than-24-characters'}
    assert client.get('/health').status_code == 200
    assert client.get('/api/state').status_code == 401
    assert client.get('/api/state', headers=demo_headers).status_code == 401
    assert client.get('/api/state', headers=headers).status_code == 200
    assert client.get('/api/receipts/not-real').status_code == 401
    assert client.post('/api/code-runs', json={'goal':'Calculate 19 plus 23'}).status_code == 401
    assert client.get('/api/demo-access', headers=demo_headers).json()['access'] == 'demo'
    queued=client.post('/api/code-runs', json={'goal':'Calculate 19 plus 23'}, headers=demo_headers)
    assert queued.status_code == 200, queued.text
    run_id=queued.json()['run_id']
    assert client.get('/api/code-runs/'+run_id, headers=demo_headers).json()['status'] == 'QUEUED'
    assert client.post('/api/demo/berlin-pantry').status_code == 401
    assert client.post('/api/demo/berlin-pantry', headers=demo_headers).status_code == 401
    assert client.post('/api/demo/berlin-pantry', headers=headers).status_code == 200
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_sandbox_payment_without_credentials_blocks_cleanly(tmp_path):
    import subprocess
    import sys
    env = os.environ.copy()
    env.update({"DATABASE_URL": f"sqlite:///{tmp_path / 'missing-provider.db'}",
                "PAYMENT_MODE": "airwallex_sandbox", "PLANNER_MODE": "deterministic",
                "SANDBOX_MODE": "local", "AW_BENEFICIARY_KAFFEE": "fictional-sandbox-beneficiary",
                "AIRWALLEX_CLIENT_ID": "", "AIRWALLEX_API_KEY": ""})
    script = """
from crew.seed import seed_demo
from crew.workflow import run_flow
seed_demo(reset=True)
result=run_flow('berlin-pantry')[0]
assert result['first_status']=='BLOCKED', result
assert result['blocked_rule']=='PROVIDER_UNAVAILABLE', result
"""
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
