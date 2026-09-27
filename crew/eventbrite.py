"""Eventbrite public RSVP publishing, called only after a bound Slack approval.

Every provider mutation is a single attempt. The caller persists each returned
ID before moving to the next step; ambiguous outcomes must be reconciled by a
person rather than retried automatically.
"""

from __future__ import annotations

import os
import re
from datetime import timezone
from urllib.parse import urlparse

import httpx

from .luma import LumaPlan, checked_luma_plan, plan_snapshot


def checked_eventbrite_plan(plan: LumaPlan, request_text: str) -> LumaPlan:
    plan = checked_luma_plan(plan, request_text)
    if plan.clarification:
        return plan
    count = str(plan.capacity) if plan.capacity is not None else ""
    stated = bool(count and re.search(
        rf"\b(?:for|capacity|limit(?:ed)? to|up to|spots?|seats?|tickets?)\s*(?:of\s*)?{count}\b"
        rf"|\b{count}\s*(?:people|attendees|spots|seats|tickets|RSVPs)\b",
        request_text, re.I,
    ))
    if not stated:
        raise ValueError("Please give an exact RSVP capacity (1–500)")
    if plan.guests:
        raise ValueError("This public RSVP flow does not email named guests; request a public page without guest emails")
    if plan.location:
        venue = os.getenv("EVENTBRITE_VENUE_LABEL", "").strip()
        if not venue or plan.location.casefold() != venue.casefold() or not os.getenv("EVENTBRITE_VENUE_ID"):
            raise ValueError("Please use the configured event venue, or give an online meeting link")
    return plan


def approval_preview(run_id: str, plan: LumaPlan) -> str:
    place = plan.location or plan.meeting_url
    snapshot = plan_snapshot(plan)
    return (
        f"Run {run_id}: Eventbrite public RSVP draft — {plan.name}. "
        f"{plan.start_at.isoformat()} to {plan.end_at.isoformat()} ({plan.timezone}); "
        f"location: {place}; description: {plan.description or 'none'}; "
        f"free RSVP capacity: {plan.capacity}. No page has been published or invitations sent. "
        f"Review snapshot {snapshot} and approve in Slack with /crew approve {run_id} {snapshot}, "
        f"or reject with /crew reject {run_id}."
    )


class EventbriteClient:
    BASE = "https://www.eventbriteapi.com/v3"

    def __init__(self, token: str | None = None, organization_id: str | None = None):
        self.token = token or os.getenv("EVENTBRITE_PRIVATE_TOKEN", "")
        self.organization_id = organization_id or os.getenv("EVENTBRITE_ORGANIZATION_ID", "")
        if not self.token or not self.organization_id.isdecimal():
            raise RuntimeError("Eventbrite token and organization ID are not configured")

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        response = httpx.request(
            method, self.BASE + path,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            json=body, timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def create_draft(self, plan: LumaPlan) -> str:
        if plan.capacity is None:
            raise ValueError("Public RSVP capacity is required")
        def utc(value):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        summary = " ".join(part for part in (plan.description, plan.meeting_url) if part)[:140]
        event = {
            "name": {"html": plan.name},
            "summary": summary or plan.name,
            "start": {"timezone": plan.timezone, "utc": utc(plan.start_at)},
            "end": {"timezone": plan.timezone, "utc": utc(plan.end_at)},
            "currency": "USD", "listed": False,
            "online_event": bool(plan.meeting_url),
        }
        if plan.location:
            event["venue_id"] = os.environ["EVENTBRITE_VENUE_ID"]
        result = self._request("POST", f"/organizations/{self.organization_id}/events/", {"event": event})
        event_id = result.get("id")
        if not isinstance(event_id, str) or not event_id.isdecimal() or result.get("status") != "draft":
            raise RuntimeError("Eventbrite draft outcome is uncertain; reconcile before retrying")
        return event_id

    def create_free_ticket(self, event_id: str, capacity: int) -> str:
        result = self._request("POST", f"/events/{event_id}/ticket_classes/", {
            "ticket_class": {"name": "Free RSVP", "free": True, "quantity_total": capacity},
        })
        ticket_id = result.get("id")
        if not isinstance(ticket_id, str) or not ticket_id.isdecimal() or result.get("free") is not True:
            raise RuntimeError("Eventbrite ticket outcome is uncertain; reconcile before retrying")
        return ticket_id

    def publish(self, event_id: str) -> str:
        self._request("POST", f"/events/{event_id}/publish/")
        event = self._request("GET", f"/events/{event_id}/")
        url = event.get("url", "")
        parsed = urlparse(url)
        if event.get("status") != "live" or parsed.scheme != "https" or parsed.hostname not in {
            "www.eventbrite.com", "eventbrite.com",
        }:
            raise RuntimeError("Eventbrite publication could not be verified; reconcile before retrying")
        return url
