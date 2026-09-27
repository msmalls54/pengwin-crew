"""Typed, non-executing proposals for multi-part office projects.

Model output is never evidence that an action happened or a detail was stated.
This module compares it with the employee's words before a plan is stored.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
EVENT_WORDS = re.compile(r"\b(event|party|gathering|meetup|meeting|launch|lunch|reception)\b", re.I)
BOTTLE_WORDS = re.compile(r"\b(water\s+bottles?|bottles?|drinkware)\b", re.I)
INVITE_WORDS = re.compile(r"\b(invite|invitation|rsvp|guest|attendee)\w*\b", re.I)
VENUE_WORDS = re.compile(r"\b(venue|location|park|restaurant|space|room|place|reserve|book)\b", re.I)

MissingCode = Literal[
    "event_date", "event_time", "event_location", "venue_availability",
    "event_capacity", "bottle_quantity", "bottle_design", "delivery_address",
    "product_quote", "invite_audience", "invitation_approval",
]


class EventProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=120)
    date_phrase: str | None = Field(default=None, max_length=80)
    time_phrase: str | None = Field(default=None, max_length=80)
    venue_name: str | None = Field(default=None, max_length=160)
    capacity: int | None = Field(default=None, ge=1, le=10000)


class SwagProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: Literal["water_bottle"] = "water_bottle"
    quantity: int | None = Field(default=None, ge=1, le=1000)
    design_phrase: str | None = Field(default=None, max_length=160)


class InvitationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["draft_invitations"] = "draft_invitations"
    emails: list[str] = Field(default_factory=list, max_length=25)
    audience_phrase: str | None = Field(default=None, max_length=160)


class MissingInformation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: MissingCode
    prompt: str = Field(min_length=1, max_length=200)


class ProjectPlan(BaseModel):
    """A reviewable proposal; it grants no permission to spend, book, or send."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["project_proposal"] = "project_proposal"
    name: str = Field(min_length=1, max_length=120)
    event: EventProposal | None = None
    swag: SwagProposal | None = None
    invitations: InvitationProposal | None = None
    clarification: str | None = Field(default=None, max_length=240)
    missing: list[MissingInformation] = Field(default_factory=list, max_length=12)


class SourcingProposal(BaseModel):
    """Read-only proposal for goods outside the fictional demo catalog."""

    model_config = ConfigDict(extra="forbid")
    kind: Literal["sourcing_proposal"] = "sourcing_proposal"
    product_phrase: str = Field(min_length=2, max_length=60)
    quantity: int = Field(ge=1, le=1000)
    status: Literal["research_only"] = "research_only"


def generic_sourcing_proposal(text: str) -> SourcingProposal | None:
    """Source only an affirmative, explicit quantity + non-catalog product."""
    demo_products = {
        "oat milk", "oat milk carton", "oat milk cartons", "coffee", "coffee bag",
        "coffee bags", "coffee beans", "hoodie", "hoodies", "welcome kit",
        "welcome kits",
    }
    clauses = re.split(
        r"[.!?;]|\bbut\b|"
        r"\band\s+(?=(?:please\s+)?(?:buy|order|purchase|get)\b)|"
        r",(?=\s*(?:(?:instead|please)\s+)?(?:buy|order|purchase|get)\b)",
        text, flags=re.I,
    )
    negative_action = re.compile(
        r"\b(?:do\s+not|don['’]?t|never|not\s+to)\s+"
        r"(?:(?:please|ever|actually|yet)\s+)?"
        r"(?:(?:want\s+(?:you\s+)?to|need\s+to)\s+)?"
        r"(?:buy|order|purchase|get)\b"
        r"|\b(?:do\s+not|don['’]?t|never)\s+(?:want|need)\b"
        r"|\bno\s+(?:buy|order|purchase|get)\b",
        re.I,
    )
    request = re.compile(
        r"\b(?:buy|order|purchase|get)\s+(?:me\s+)?(\d{1,4})\s+"
        r"([a-z][a-z -]{1,60}?)(?=\s+(?:for|to|at|with|and|from|instead)\b|$)",
        re.I,
    )
    for clause in clauses:
        if negative_action.search(clause):
            continue
        for match in request.finditer(clause):
            quantity = int(match.group(1))
            product = " ".join(match.group(2).casefold().removesuffix(" please").split())
            if product and product not in demo_products and 1 <= quantity <= 1000:
                return SourcingProposal(product_phrase=product, quantity=quantity)
    return None


def user_request_source(request_text: str, context: str = "") -> str:
    """Keep only prior USER turns; past agent prose is not evidence of intent."""
    if not request_text.strip() or len(request_text) > 1000 or len(context) > 4000:
        raise ValueError("Project request is missing or too long")
    user_lines = []
    for line in context.splitlines():
        if line.startswith("USER:"):
            user_lines.append(line.removeprefix("USER:").strip())
    return "\n".join([*user_lines, request_text.strip()])[-4000:]


def _clean(value: str) -> str:
    return " ".join(value.casefold().split())


def _is_stated(phrase: str | None, source: str) -> bool:
    return bool(phrase and _clean(phrase) in _clean(source))


def _stated_numbers_near(source: str, subject: re.Pattern[str]) -> set[int]:
    """Choose the closest number to each subject, not any number in the plan."""
    numbers: set[int] = set()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
             "eleven": 11, "twelve": 12, "fifteen": 15, "twenty": 20,
             "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
             "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
    candidates: list[tuple[int, int]] = [
        (match.start(), int(match.group())) for match in
        re.finditer(r"(?<!\w)\d{1,5}(?!\w)", source)
    ]
    for word, value in words.items():
        candidates.extend((match.start(), value) for match in re.finditer(rf"\b{word}\b", source, re.I))
    for match in subject.finditer(source):
        ranked = sorted(
            (min(abs(position - match.start()), abs(position - match.end())), value)
            for position, value in candidates
            if abs(position - match.start()) <= 35 or abs(position - match.end()) <= 35
        )
        if ranked and (len(ranked) == 1 or ranked[0][0] < ranked[1][0]):
            numbers.add(ranked[0][1])
    return numbers


def _stated_capacity(source: str) -> set[int]:
    number = r"(\d{1,5}|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety|hundred)"
    patterns = (
        rf"\b(?:capacity|seats?|tickets?|spots?)\s*(?:of|for|:)?\s*{number}\b",
        rf"\b{number}\s+(?:guests?|attendees?|people|seats?|spots?|tickets?)\b",
        rf"\bfor\s+{number}\s+(?:(?:local|ai|startup|tech|community|business)\s+){{0,3}}founders?\b",
    )
    values: set[int] = set()
    words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twenty": 20,
             "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60,
             "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100}
    for pattern in patterns:
        for match in re.finditer(pattern, source, re.I):
            raw = match.group(1).casefold()
            values.add(int(raw) if raw.isdigit() else words[raw])
    return values


def _stated_start_and_duration(source: str) -> str | None:
    """Preserve an explicitly stated start and duration if the model shortens it."""
    match = re.search(
        r"\b(?:starting|starts?|from)\s+(?:around\s+)?\d{1,2}(?::\d{2})?\s*"
        r"(?:a\.?m\.?|p\.?m\.?)"
        r"(?:\s+for\s+\d{1,3}\s+(?:minutes?|hours?))?",
        source, re.I,
    )
    return match.group(0) if match else None


def _stated_park_venue(source: str) -> str | None:
    match = re.search(r"\bat\s+([A-Z][A-Za-z0-9 .'-]{1,80}?\s+Park)\b", source)
    return " ".join(match.group(1).split()) if match else None


def _stated_founder_audience(source: str) -> str | None:
    match = re.search(
        r"\bfor\s+\d{1,5}\s+((?:(?:local|ai|startup|tech|community|business)\s+){0,3}founders?)\b",
        source, re.I,
    )
    return match.group(1) if match else None


def _missing(code: MissingCode, prompt: str) -> MissingInformation:
    return MissingInformation(code=code, prompt=prompt)


def checked_project_plan(plan: ProjectPlan, request_text: str, context: str = "") -> ProjectPlan:
    """Drop unsupported model specifics, then derive typed information gaps."""
    source = user_request_source(request_text, context)
    event_requested = bool(EVENT_WORDS.search(source))
    bottle_requested = bool(BOTTLE_WORDS.search(source))
    invite_requested = bool(INVITE_WORDS.search(source))
    if not (event_requested or bottle_requested or invite_requested):
        raise ValueError("The request contains no supported project component")

    event = plan.event if event_requested else None
    if event_requested and event is None:
        event = EventProposal(title="Event")
    if event:
        stated_capacities = _stated_capacity(source)
        relative_date = re.search(
            r"\b(?:about a month from now|in about a month|around a month from now|next month)\b",
            source, re.I,
        )
        event = event.model_copy(update={
            "title": event.title if _is_stated(event.title, source) else "Event",
            "date_phrase": (event.date_phrase if _is_stated(event.date_phrase, source) else
                            relative_date.group(0) if relative_date else None),
            "time_phrase": (_stated_start_and_duration(source) or
                            (event.time_phrase if _is_stated(event.time_phrase, source) else None)),
            "venue_name": (event.venue_name if _is_stated(event.venue_name, source) and VENUE_WORDS.search(source)
                           else _stated_park_venue(source)),
            "capacity": (event.capacity if event.capacity in stated_capacities else
                         next(iter(stated_capacities)) if len(stated_capacities) == 1 else None),
        })

    swag = plan.swag if bottle_requested else None
    if bottle_requested and swag is None:
        swag = SwagProposal()
    if swag:
        stated_quantities = _stated_numbers_near(source, BOTTLE_WORDS)
        swag = swag.model_copy(update={
            "quantity": (swag.quantity if swag.quantity in stated_quantities else
                         next(iter(stated_quantities)) if len(stated_quantities) == 1 else None),
            "design_phrase": swag.design_phrase if _is_stated(swag.design_phrase, source) else None,
        })

    invitations = plan.invitations if invite_requested else None
    if invite_requested and invitations is None:
        invitations = InvitationProposal()
    if invitations:
        mentioned_emails = {email.casefold() for email in EMAIL.findall(source)}
        emails = list(dict.fromkeys(email.strip().casefold() for email in invitations.emails
                                    if email.strip().casefold() in mentioned_emails))
        invitations = invitations.model_copy(update={
            "emails": emails,
            "audience_phrase": (invitations.audience_phrase if _is_stated(invitations.audience_phrase, source)
                                else _stated_founder_audience(source)),
        })

    missing: list[MissingInformation] = []
    if event:
        if not event.date_phrase:
            missing.append(_missing("event_date", "What date should the event be?"))
        if not event.time_phrase:
            missing.append(_missing("event_time", "What time should it start, and when should it end?"))
        if not event.venue_name:
            missing.append(_missing("event_location", "What venue or online location should I use?"))
        else:
            missing.append(_missing("venue_availability", "Can you confirm the venue is available and approved for booking?"))
        if invite_requested and event.capacity is None:
            missing.append(_missing("event_capacity", "What RSVP capacity should I set?"))
    if swag:
        if swag.quantity is None:
            missing.append(_missing("bottle_quantity", "How many water bottles do you need?"))
        if not swag.design_phrase:
            missing.append(_missing("bottle_design", "What design and bottle type should I use?"))
        missing.extend((
            _missing("delivery_address", "What delivery address should the bottles go to?"),
            _missing("product_quote", "I need a current product and shipping quote before any purchase."),
        ))
    if invitations:
        if not invitations.emails and not invitations.audience_phrase:
            missing.append(_missing("invite_audience", "Who should receive the invitation?"))
        missing.append(_missing("invitation_approval", "Please review the invitation text and delivery channel before anything is sent."))

    name = (plan.name if _is_stated(plan.name, source) else
            event.title if event and event.title != "Event" else
            f"Event at {event.venue_name}"[:120] if event and event.venue_name else
            "Office project")
    # A model-written clarification could claim fabricated facts or approvals.
    return ProjectPlan(name=name, event=event, swag=swag, invitations=invitations, missing=missing)


def project_plan_summary(plan: ProjectPlan, research: object | None = None) -> str:
    """Explain the proposal without turning proposed work into completed acts."""
    generic_titles = {"event", "gathering", "pengwin event", "pengwin gathering"}
    event_label = (plan.event.title if plan.event and
                   plan.event.title.casefold() not in generic_titles else None)
    project_label = (event_label or
                     (f"the event at {plan.event.venue_name}" if plan.event and plan.event.venue_name else None) or
                     (plan.name if plan.name != "Office project" else "your project"))
    pieces = [f"Saved the plan for {project_label}."]
    if plan.event:
        details = [value for value in (plan.event.date_phrase, plan.event.time_phrase, plan.event.venue_name) if value]
        if plan.event.capacity:
            details.append(f"capacity {plan.event.capacity}")
        pieces.append(f"Event: {', '.join(details) if details else 'date and venue to confirm'}.")
    if plan.swag:
        count = str(plan.swag.quantity) if plan.swag.quantity else "quantity to confirm"
        pieces.append(f"Water bottles: {count}; Buyer is checking the product source and price.")
    if plan.invitations:
        audience = plan.invitations.audience_phrase or (f"{len(plan.invitations.emails)} named recipient(s)" if plan.invitations.emails else "audience to confirm")
        pieces.append(f"Invitations: Events is drafting copy for {audience}.")
    pieces.append("Treasurer is reviewing the budget purpose and any product estimate.")
    status = getattr(research, "status", None)
    if status == "unavailable":
        pieces.append("Live search is unavailable, so current venue and product details are unverified.")
    elif status == "error":
        pieces.append("Live search failed; current venue and product details are unverified.")
    if plan.missing:
        questions = [item.prompt for item in plan.missing if item.code not in {
            "venue_availability", "product_quote", "invitation_approval",
        }]
        if questions:
            pieces.append("Next: " + " ".join(questions[:3]))
    pending_actions = []
    if plan.event:
        pending_actions.append("venue booking or Eventbrite publication")
    if plan.swag:
        pending_actions.append("order or payment")
    if plan.invitations:
        pending_actions.append("invitation send")
    pieces.append("Status: planning only; no " + ", ".join(pending_actions) + ".")
    return "\n".join(pieces)[:1800]


def is_multi_part_project_request(text: str, context: str = "") -> bool:
    """Route compound requests before the single-purpose demo flows."""
    try:
        source = user_request_source(text, context)
    except ValueError:
        return False
    is_action = bool(re.search(
        r"\b(plan|organize|arrange|set up|create|order|buy|purchase|add|need|want|invite|source|get|research)\b",
        text, re.I,
    ))
    direct_compound = bool(EVENT_WORDS.search(text)
                           and (BOTTLE_WORDS.search(text) or INVITE_WORDS.search(text)))
    explicit_followup = bool(re.match(r"^\s*(and|also)\b", text, re.I)
                             and EVENT_WORDS.search(source)
                             and (BOTTLE_WORDS.search(text) or INVITE_WORDS.search(text)))
    return bool((is_action and direct_compound) or explicit_followup)
