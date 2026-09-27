"""Luma event drafts and the approval-gated provider adapter.

The model may suggest an event, but only an approved, immutable draft reaches
this adapter. The calendar key stays in the Events control worker, never in a
browser or code sandbox.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import BaseModel, ConfigDict, Field


EMAIL = re.compile(r"^[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}$", re.IGNORECASE)


class LumaPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, max_length=120)
    start_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=80)
    description: str = Field(default="", max_length=1000)
    location: str | None = Field(default=None, max_length=240)
    meeting_url: str | None = Field(default=None, max_length=500)
    guests: list[str] = Field(default_factory=list, max_length=25)
    capacity: int | None = Field(default=None, ge=1, le=500)
    clarification: str | None = Field(default=None, max_length=240)


def checked_luma_plan(plan: LumaPlan, request_text: str) -> LumaPlan:
    """Reject invented recipients and incomplete or stale event details."""
    if plan.clarification:
        return plan
    if not plan.name or not plan.name.strip() or not plan.start_at or not plan.end_at or not plan.timezone:
        raise ValueError("Please give the event name, date, start and end times, and time zone")
    if plan.start_at.tzinfo is None or plan.end_at.tzinfo is None:
        raise ValueError("Event times need a time zone")
    try:
        ZoneInfo(plan.timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("Event time zone is not recognized") from exc
    if plan.start_at <= datetime.now(timezone.utc) or not plan.start_at < plan.end_at:
        raise ValueError("Event times must be in the future and in order")
    if (plan.end_at - plan.start_at).total_seconds() > 12 * 3600:
        raise ValueError("Event duration exceeds twelve hours")
    if bool(plan.location) == bool(plan.meeting_url):
        raise ValueError("Specify either an in-person location or an online meeting link")
    if plan.meeting_url and (not plan.meeting_url.startswith("https://") or plan.meeting_url not in request_text):
        raise ValueError("The meeting link must be an HTTPS URL in the request")
    found = {value.casefold() for value in re.findall(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", request_text, re.I)}
    guests = [guest.strip().casefold() for guest in plan.guests]
    if len(guests) != len(set(guests)) or any(not EMAIL.fullmatch(guest) or guest not in found for guest in guests):
        raise ValueError("Guest emails must be unique and explicitly present in the request")
    if found != set(guests):
        raise ValueError("Every email in the request must appear in the guest list")
    return plan.model_copy(update={"name": plan.name.strip(), "guests": guests})


def plan_snapshot(plan: LumaPlan) -> str:
    canonical = json.dumps(plan.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:12]


def event_time_label(plan: LumaPlan) -> str:
    zone = ZoneInfo(plan.timezone)
    start = plan.start_at.astimezone(zone)
    end = plan.end_at.astimezone(zone)

    def clock(value: datetime) -> str:
        hour = value.hour % 12 or 12
        minute = f":{value.minute:02d}" if value.minute else ""
        return f"{hour}{minute} {'AM' if value.hour < 12 else 'PM'}"

    day = f"{start:%A, %B} {start.day}, {start.year}"
    if start.date() == end.date():
        return f"{day}, {clock(start)}–{clock(end)} {end:%Z}"
    return f"{day}, {clock(start)} {start:%Z} to {end:%A, %B} {end.day}, {clock(end)} {end:%Z}"


def approval_preview(run_id: str, plan: LumaPlan) -> str:
    guests = ", ".join(plan.guests) if plan.guests else "none"
    place = f"Online: {plan.meeting_url}" if plan.meeting_url else f"At: {plan.location}"
    description = f"\nAbout: {plan.description}" if plan.description else ""
    return (
        "🐧 Here's the calendar draft. Nothing has been created yet.\n"
        f"{plan.name}\n"
        f"When: {event_time_label(plan)}\n"
        f"{place}\n"
        f"Guests: {guests}{description}\n\n"
        "Reply in this thread with @Pengwin Events approve this event, or @Pengwin Events cancel this draft."
    )


class LumaClient:
    BASE = "https://public-api.luma.com"

    def __init__(self, key: str | None = None):
        self.key = key or os.getenv("LUMA_API_KEY", "")
        if not self.key:
            raise RuntimeError("Luma calendar API key is not configured")

    def _post(self, path: str, body: dict) -> dict:
        response = httpx.post(
            self.BASE + path,
            headers={"x-luma-api-key": self.key, "accept": "application/json"},
            json=body,
            timeout=30,
        )
        response.raise_for_status()
        return response.json()

    def create_event(self, plan: LumaPlan) -> str:
        body = {
            "name": plan.name,
            "start_at": plan.start_at.isoformat(),
            "end_at": plan.end_at.isoformat(),
            "timezone": plan.timezone,
            "description_md": plan.description,
            "visibility": "private",
            "registration_open": True,
        }
        if plan.location:
            body["geo_address_json"] = {"type": "manual", "address": plan.location}
        else:
            body["meeting_url"] = plan.meeting_url
        event_id = self._post("/v1/events/create", body).get("id")
        if not isinstance(event_id, str) or not event_id.startswith("evt-"):
            raise RuntimeError("Luma did not return an event ID; reconcile before retrying")
        return event_id

    def send_invites(self, event_id: str, guests: list[str]) -> list[str]:
        if not guests:
            return []
        result = self._post("/v1/events/guests/send-invites", {
            "event_id": event_id, "guests": [{"email": email} for email in guests],
        })
        return [item["email"] for item in result.get("skipped", []) if isinstance(item, dict) and "email" in item]
