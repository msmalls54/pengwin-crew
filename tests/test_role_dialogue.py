import os
import subprocess
import sys


def test_direct_role_chat_routes_and_reports_actual_ledger(tmp_path):
    env = os.environ.copy()
    env.update({
        "DATABASE_URL": f"sqlite:///{tmp_path / 'role-chat.db'}",
        "PAYMENT_MODE": "simulated",
        "SLACK_DEMO_CHANNEL_ID": "C-demo",
        "SLACK_ALLOWED_USER_IDS": "U-owner",
        "SLACK_REPORT_TIMEZONE": "America/Los_Angeles",
    })
    script = r'''
from datetime import datetime, timezone
from uuid import uuid4
from crew import dialogue
from crew.db import Payment, PendingOrder, Request, SessionLocal
from crew.seed import seed_demo

seed_demo(reset=True)
now = datetime(2026, 9, 27, 2, 0, tzinfo=timezone.utc)  # Sep 26 in Pacific time
with SessionLocal.begin() as session:
    request_id = str(uuid4())
    order_id = str(uuid4())
    session.add(Request(id=request_id, source='slack', source_user='U-owner', text='demo',
                        office_id='BER', category='office-supplies', sku='COFFEE', requested_qty=1))
    session.add(PendingOrder(id=order_id, request_id=request_id, vendor_id='kaffee-kontor',
                             sku='COFFEE', qty=1, amount_cents=5800, currency='EUR'))
    session.add(Payment(id=str(uuid4()), pending_order_id=order_id, request_id=request_id,
                        amount_cents=5800, currency='EUR', method='simulated', status='SIMULATED',
                        simulated=True, created_at=now))

report = dialogue.spend_report('how much have we spent today?', now=now)
assert '€58.00' in report, report
assert 'recorded today:' in report and 'demo checkout' in report, report
assert '€58.00' not in dialogue.spend_report('yesterday', now=now)

dialogue.spend_report = lambda text: report
answer = dialogue.answer('Treasurer', 'how much have we spent today?', user_id='U-owner',
                         channel_id='C-demo', delivery_id='event:1')
assert '€58.00' in answer and 'demo checkout' in answer
assert '€58.00' in dialogue.answer('Treasurer', 'How much did Pengwin spend today?',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:global-spend')
assert "won't send money" in dialogue.answer('Treasurer', 'please transfer me €10',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:2')

calls = []
dialogue.queue_allowed_flow = lambda flow, **kwargs: calls.append((flow, kwargs)) or 'queued'
assert dialogue.answer('Buyer', 'please order 3 oat milk cartons for Berlin',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:3') == 'queued'
assert calls[-1][0] == 'natural-language'
assert calls[-1][1]['request_text'] == 'please order 3 oat milk cartons for Berlin'
assert not dialogue._looks_like_purchase('Please do not order.')
assert not dialogue._looks_like_purchase('Please don\'t buy 24 water bottles.')
assert not dialogue._looks_like_purchase('I do not want to buy 3 oat milk cartons.')
assert dialogue._looks_like_purchase('Please order 3 oat milk cartons for Berlin')
assert dialogue._looks_like_purchase('Don\'t forget to order 3 oat milk cartons')
assert dialogue._looks_like_purchase('Do not order 24 bottles; buy 2 coffee bags instead')
prior = 'USER: Which Pengwin RSVP events are currently live, especially on Oct 2?\nPENGWIN Events: Checking.'
assert dialogue.answer('Events', 'And on Oct 2nd?', context=prior,
    user_id='U-owner', channel_id='C-demo', delivery_id='event:oct-followup') == 'queued'
assert calls[-1][0] == 'event-status'
assert calls[-1][1]['context'] == prior
assert dialogue._looks_like_event_status('What about Oct 2nd?', prior)
assert dialogue._looks_like_event_status('How about on Oct 2nd?', prior)
assert not dialogue._looks_like_event_status('And on Oct 2nd?', 'USER: Plan a new event')
assert dialogue.answer('Events', 'set up a Berlin team lunch for 5',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:4') == 'queued'

class StubInference:
    def role_reply(self, **kwargs):
        return f"Hello from {kwargs['role']}"
dialogue.VultrInference = StubInference
assert dialogue.answer('Buyer', 'hey, what can you do?',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:5') == 'Hello from Buyer'
before = len(calls)
assert dialogue.answer('Buyer', 'Please do not order 24 oat milk cartons.',
    user_id='U-owner', channel_id='C-demo', delivery_id='event:no-order') == 'Hello from Buyer'
assert len(calls) == before
'''
    result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
