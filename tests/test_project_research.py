import os
import subprocess
import sys

from crew.project_planner import (
    EventProposal, InvitationProposal, ProjectPlan, SwagProposal,
    checked_project_plan, is_multi_part_project_request, project_plan_summary,
)


def _run_isolated(script, tmp_path):
    env = os.environ.copy()
    env["DATABASE_URL"] = f"sqlite:///{tmp_path / 'isolated.db'}"
    result = subprocess.run([sys.executable, "-c", script], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


def test_project_plan_keeps_stated_details_and_removes_invented_ones():
    request = (
        "Plan a launch event on Oct 2 at Salesforce Park for 50 guests. "
        "Order 30 water bottles and invite the team."
    )
    proposal = ProjectPlan(
        name="launch event",
        event=EventProposal(title="launch event", date_phrase="Oct 2",
                            time_phrase="noon", venue_name="Salesforce Park", capacity=30),
        swag=SwagProposal(quantity=50, design_phrase="penguin logo"),
        invitations=InvitationProposal(emails=["invented@example.com"], audience_phrase="the team"),
    )
    checked = checked_project_plan(proposal, request)
    assert checked.event.date_phrase == "Oct 2"
    assert checked.event.venue_name == "Salesforce Park"
    assert checked.event.time_phrase is None
    assert checked.event.capacity is None  # 30 was tied to bottles, not guests.
    assert checked.swag.quantity is None  # 50 was tied to guests, not bottles.
    assert checked.swag.design_phrase is None
    assert checked.invitations.emails == []
    assert checked.invitations.audience_phrase == "the team"
    assert {"event_capacity", "bottle_quantity"} <= {item.code for item in checked.missing}
    summary = project_plan_summary(checked)
    assert "No venue" not in summary  # Specific wording is about this plan's actions.
    assert "has not reserved" in summary and "None have been sent" in summary


def test_project_context_uses_user_lines_only_and_routes_followup():
    context = "USER: Plan an event at Salesforce Park\nPENGWIN Events: The date is Nov 3"
    proposal = ProjectPlan(name="Office project", event=EventProposal(title="Event", date_phrase="Nov 3"),
                           swag=SwagProposal(quantity=20))
    checked = checked_project_plan(proposal, "Also add 20 water bottles", context)
    assert checked.event.date_phrase is None
    assert checked.swag.quantity == 20
    assert is_multi_part_project_request("Also add 20 water bottles", context)
    assert not is_multi_part_project_request("What is the status of my event and water bottles?")


def test_events_live_status_and_project_requests_use_read_only_queues(tmp_path):
    _run_isolated(r'''
from crew import dialogue
calls = []
dialogue.queue_allowed_flow = lambda flow, **kwargs: calls.append((flow, kwargs)) or "queued"
base = dict(user_id="U-owner", channel_id="C-demo", delivery_id="event:1")
assert dialogue.answer("Events", "What events do we currently have live?", **base) == "queued"
assert calls[-1][0] == "event-status"
context = "USER: What events do we currently have live?\nPENGWIN Events: Checking."
assert dialogue.answer("Events", "on Oct 2nd", context=context, **base) == "queued"
assert calls[-1][0] == "event-status"
assert calls[-1][1]["context"] == context
assert dialogue.answer("Concierge", "Plan an event and order 20 water bottles", **base) == "queued"
assert calls[-1][0] == "project-plan"
assert not dialogue._looks_like_event_status("on Oct 2nd", "USER: Plan a new event")
''', tmp_path)


def test_all_roles_get_unverified_search_context_and_generic_sourcing_is_read_only(tmp_path):
    _run_isolated(r'''
from crew import dialogue
from crew.research import ResearchResult, SearchLead
calls = []
model_calls = []
def search(query, **kwargs):
    calls.append(query)
    return ResearchResult(status="ok", kind="web", query=query,
                          results=[SearchLead(title="Supplier", url="https://example.com/chairs",
                                              snippet="Listing only")])
dialogue.lookup_project_facts = search
dialogue.spend_report = lambda _: "No spending recorded."
dialogue.status_text = lambda **_: "Budget unverified."
class StubInference:
    def role_reply(self, **kwargs):
        model_calls.append(kwargs)
        return "Here are possible suppliers; check details directly."
dialogue.VultrInference = StubInference
base = dict(user_id="U-owner", channel_id="C-demo", delivery_id="event:1")
for role in ("Concierge", "Buyer", "Events", "Treasurer"):
    answer = dialogue.answer(role, "Where can I find office chairs?", **base)
    assert "possible suppliers" in answer
    assert "Unverified search leads" in model_calls[-1]["research_context"]
assert calls == ["office chairs business suppliers"] * 4
dialogue.answer("Buyer", "Hi", **base)
assert len(calls) == 4
reply = dialogue.answer("Buyer", "Buy 5 chairs", **base)
assert "have not placed an order" in reply
assert "business checkout path" in reply
assert len(model_calls) == 5  # Sourcing did not enter model chat or mock checkout.
assert calls[-1] == "office chairs business suppliers"
''', tmp_path)


def test_search_budget_and_publisher_boundaries(tmp_path):
    _run_isolated(r'''
from types import SimpleNamespace
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from crew import research
from crew.db import ControlFlag

research.settings = SimpleNamespace(brave_key="test-key", brave_max_searches=1)
reserve = research._reserve_search
research._reserve_search = lambda: True
calls = []
class Response:
    status_code = 200
    def raise_for_status(self): pass
    def json(self):
        return {"web": {"results": [
            {"title": "<b>First</b>", "url": "https://example.com/one", "description": "Lead only"},
            {"title": "Second", "url": "https://example.com/two", "description": "Unverified"},
        ]}}
def fake_get(url, **kwargs):
    calls.append((url, kwargs))
    return Response()
research.httpx.get = fake_get
result = research.lookup_project_facts("custom water bottles for event", max_results=1)
assert result.status == "ok" and len(result.results) == 1
assert result.results[0].title == "First" and result.results[0].checked_at is None
assert result.searched_at is not None and calls[0][1]["params"]["count"] == 1
assert research.lookup_project_facts("send jane@example.com bottles").status == "unavailable"
assert len(calls) == 1

engine = create_engine("sqlite:///:memory:")
ControlFlag.__table__.create(engine)
factory = sessionmaker(engine, expire_on_commit=False, class_=Session)
with factory.begin() as session:
    session.add(ControlFlag(key="brave_searches", value="0"))
research.SessionLocal = factory
research._reserve_search = reserve
assert research._reserve_search()
assert not research._reserve_search()
with factory() as session:
    assert session.get(ControlFlag, "brave_searches").value == "1"

class Stream:
    def __enter__(self): return self
    def __exit__(self, *_): pass
    def raise_for_status(self): pass
    def iter_bytes(self):
        yield b"Our water bottles cost between $20.25 and $23.41. Shipping depends on destination."
research.httpx.stream = lambda *_, **__: Stream()
fact = research.check_printful_water_bottle_prices()
assert fact.status == "ok" and (fact.min_price, fact.max_price) == (20.25, 23.41)
assert fact.checked_at is not None and "shipping" in fact.note.lower()
''', tmp_path)
