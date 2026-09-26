"""Check the sandbox payout boundary against Airwallex's current API shape."""

import os
import subprocess
import sys


def test_airwallex_sandbox_uses_current_transfers_endpoint_and_id():
    # Settings is loaded at import time; isolate this test so it cannot change
    # the environment-sensitive workflow tests in the parent pytest process.
    script = """
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
from crew import payments

payments.settings = SimpleNamespace(
    airwallex_client_id='test-client', airwallex_api_key='test-key',
    airwallex_base='https://api.sandbox.airwallex.com')
provider = payments.AirwallexSandboxProvider()
provider._token = 'test-access-token'
provider._expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
calls = []

def fake_post(url, *, headers, json, timeout):
    calls.append((url, headers, json, timeout))
    return httpx.Response(201, json={'id': 'sandbox-transfer-1', 'status': 'SCHEDULED'},
                          request=httpx.Request('POST', url))

payments.httpx.post = fake_post
assert provider.submit(payment_id='demo-123', amount_cents=1920, currency='EUR',
                       beneficiary_id='sandbox-beneficiary-1') == ('sandbox-transfer-1', 'SCHEDULED')
url, headers, body, timeout = calls[0]
assert url == 'https://api.sandbox.airwallex.com/api/v1/transfers/create'
assert headers['Authorization'] == 'Bearer test-access-token'
assert body == {
    'beneficiary_id': 'sandbox-beneficiary-1',
    'transfer_amount': '19.20', 'transfer_currency': 'EUR',
    'source_currency': 'EUR', 'transfer_method': 'LOCAL',
    'reason': 'office supplies', 'reference': 'CREW-demo-123',
    'request_id': 'demo-123',
}
assert timeout == 30
"""
    result = subprocess.run([sys.executable, "-c", script], env=os.environ.copy(),
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
