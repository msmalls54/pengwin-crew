"""Bounded, read-only Brave search for project research leads.

Search snippets are untrusted leads, never proof of price, availability, venue
reservation, or a completed purchase. Results are returned in memory only.
"""

from __future__ import annotations

import html
import ipaddress
import re
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Integer, String, cast, update

from .config import settings
from .db import ControlFlag, SessionLocal


SearchKind = Literal["web", "place"]
SearchStatus = Literal["ok", "unavailable", "limited", "error"]
ENDPOINTS = {
    "web": "https://api.search.brave.com/res/v1/web/search",
    "place": "https://api.search.brave.com/res/v1/local/place_search",
}
EMAIL = re.compile(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", re.I)
LONG_NUMBER = re.compile(r"(?<!\d)\d{7,}(?!\d)")
STREET_ADDRESS = re.compile(
    r"\b\d{1,6}(?:[A-Z]|\s*[-/]\s*[A-Z0-9]{1,3})?\s+"
    r"(?:[A-Z0-9.'-]+\s+){1,7}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Way|Court|Ct|Place|Pl|Terrace|Ter|Circle|Cir|Parkway|Pkwy|"
    r"Highway|Hwy|Square|Sq)\b\.?(?!\w)", re.I,
)
POST_OFFICE_BOX = re.compile(
    r"\b(?:P\s*\.?\s*O\s*\.?\s*Box|Post\s+Office\s+Box|Postfach)"
    r"\s*(?:#|No\.?|Number)?\s*\d{1,8}[A-Z]?\b", re.I,
)
STREET_ADDRESS_REVERSED = re.compile(
    r"\b(?:[A-Z0-9.'-]+\s+){1,7}"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|"
    r"Way|Court|Ct|Place|Pl|Terrace|Ter|Circle|Cir|Parkway|Pkwy|"
    r"Highway|Hwy|Square|Sq)\.?\s+\d{1,6}[A-Z]?\b", re.I,
)
GERMAN_STREET_ADDRESS = re.compile(
    r"\b[A-ZÄÖÜa-zäöüß0-9.'-]{2,60}(?:straße|strasse|str\.?|weg|platz|allee)"
    r"\s+\d{1,6}[A-Z]?\b", re.I,
)
PHONE_NUMBER = re.compile(
    r"(?<!\w)(?:\+?1[\s.()-]*)?\(?\d{3}\)?[\s.-]*\d{3}[\s.-]*\d{4}(?!\w)"
    r"|(?<!\w)\+\d{1,3}[\s.()-]*(?:\d[\s.()-]*){7,14}(?!\w)"
)
HTML_TAG = re.compile(r"<[^>]*>")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class SearchLead(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    url: str | None = Field(default=None, max_length=500)
    snippet: str = Field(default="", max_length=300)
    # None until the publisher's own page is checked separately.
    checked_at: datetime | None = None


class ResearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: SearchStatus
    kind: SearchKind
    query: str = Field(max_length=220)
    location: str | None = Field(default=None, max_length=120)
    results: list[SearchLead] = Field(default_factory=list, max_length=5)
    searched_at: datetime | None = None
    reason: str | None = Field(default=None, max_length=160)


class PublisherFact(BaseModel):
    """A fact read from a fixed official publisher page, never a checkout quote."""

    model_config = ConfigDict(extra="forbid")
    status: Literal["ok", "unavailable", "error"]
    source_url: str
    checked_at: datetime | None = None
    currency: Literal["USD"] | None = None
    min_price: float | None = None
    max_price: float | None = None
    note: str = ""


def _clean_text(value: object, limit: int) -> str:
    raw = str(value or "")
    raw = HTML_TAG.sub(" ", raw)
    raw = CONTROL.sub(" ", html.unescape(raw))
    return " ".join(raw.split())[:limit]


def _public_url(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 500:
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.scheme not in {"https", "http"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    hostname = parsed.hostname.casefold()
    if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
        return None
    try:
        if not ipaddress.ip_address(hostname).is_global:
            return None
    except ValueError:
        pass
    return value


def _reserve_search() -> bool:
    """Count an attempted query before dispatch, including timeouts."""
    with SessionLocal.begin() as session:
        value = cast(ControlFlag.value, Integer)
        result = session.execute(
            update(ControlFlag)
            .where(ControlFlag.key == "brave_searches", value < settings.brave_max_searches)
            .values(value=cast(value + 1, String(100)))
        )
        # A single conditional UPDATE is atomic in SQLite and Postgres.
        return result.rowcount == 1


def _contains_private_contact(value: str) -> bool:
    return bool(EMAIL.search(value) or LONG_NUMBER.search(value)
                or STREET_ADDRESS.search(value) or STREET_ADDRESS_REVERSED.search(value)
                or GERMAN_STREET_ADDRESS.search(value) or POST_OFFICE_BOX.search(value)
                or PHONE_NUMBER.search(value))


def event_venue_queries(venue_name: str) -> tuple[str, str | None, str]:
    """Use fixed public venue/city terms; never send an arbitrary venue label."""
    name = " ".join(venue_name.split())
    if re.fullmatch(r"(?:the\s+)?Salesforce Park(?:,?\s+San Francisco(?:,?\s+CA)?)?", name, re.I):
        return ("Salesforce Park", "san francisco ca united states",
                "Salesforce Park official event reservation permit")
    if re.search(r"\b(?:San Francisco|SF)\b", name, re.I):
        return ("event venues", "san francisco ca united states",
                "San Francisco event venue reservation permit guidance")
    if re.search(r"\bBerlin\b", name, re.I):
        return ("event venues", "berlin germany",
                "Berlin event venue reservation permit guidance")
    return ("event venues", None, "event venue reservation permit guidance")


def _extract_leads(data: dict, kind: SearchKind, max_results: int) -> list[SearchLead]:
    if kind == "web":
        section = data.get("web")
        if section is None:
            return []
        if not isinstance(section, dict):
            raise ValueError("Unexpected web results section")
        raw = section.get("results")
    else:
        raw = data.get("results")
        if isinstance(raw, dict):
            raw = raw.get("results")
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError("Unexpected search results section")
    leads: list[SearchLead] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = _clean_text(item.get("title") or item.get("name"), 160)
        if not title:
            continue
        url = _public_url(item.get("url") or item.get("website"))
        key = url or title.casefold()
        if key in seen:
            continue
        seen.add(key)
        address = item.get("postal_address")
        display_address = address.get("displayAddress") if isinstance(address, dict) else None
        snippet = _clean_text(item.get("description") or display_address or item.get("meta_description"), 300)
        leads.append(SearchLead(title=title, url=url, snippet=snippet))
        if len(leads) >= max_results:
            break
    return leads


def lookup_project_facts(query: str, *, kind: SearchKind = "web",
                         location: str | None = None,
                         max_results: int = 3) -> ResearchResult:
    """Return search leads without persisting raw results or claiming verification.

    Personal contact details and street addresses are rejected rather than
    sent to Brave.
    Callers should use short generic queries, not invitee lists or delivery
    addresses. The only durable side effect is the budget counter.
    """
    if kind not in ENDPOINTS:
        raise ValueError("Unsupported search kind")
    query = " ".join(query.split())
    location = " ".join(location.split()) if location else None
    if (not query or len(query) > 220 or _contains_private_contact(query)
            or (location and (len(location) > 120 or _contains_private_contact(location)))):
        return ResearchResult(status="unavailable", kind=kind, query="[withheld]",
                              location="[withheld]" if location else None,
                              reason="Search query contains personal details or is too long")
    if not settings.brave_key:
        return ResearchResult(status="unavailable", kind=kind, query=query, location=location,
                              reason="Brave Search is not configured")
    if not 1 <= max_results <= 5:
        raise ValueError("max_results must be from 1 to 5")
    try:
        if not _reserve_search():
            return ResearchResult(status="limited", kind=kind, query=query, location=location,
                                  reason="Search budget is exhausted or unavailable")
    except Exception:
        return ResearchResult(status="error", kind=kind, query=query, location=location,
                              reason="Search budget could not be checked")
    try:
        params = {"q": query, "count": max_results}
        if kind == "place" and location:
            params["location"] = location
        response = httpx.get(
            ENDPOINTS[kind],
            headers={"X-Subscription-Token": settings.brave_key, "Accept": "application/json"},
            params=params,
            timeout=15,
            follow_redirects=False,
        )
        if response.status_code == 429:
            return ResearchResult(status="limited", kind=kind, query=query, location=location,
                                  reason="Search service rate limit")
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("Unexpected search response")
        return ResearchResult(status="ok", kind=kind, query=query, location=location,
                              results=_extract_leads(data, kind, max_results),
                              searched_at=datetime.now(timezone.utc))
    except (httpx.HTTPError, ValueError, TypeError):
        return ResearchResult(status="error", kind=kind, query=query, location=location,
                              reason="Search service did not return usable results")


def check_printful_water_bottle_prices() -> PublisherFact:
    """Read the fixed official catalog page; do not infer shipping or stock.

    The URL is fixed to avoid fetching user-provided hosts from a worker.
    """
    url = "https://www.printful.com/custom-water-bottles"
    try:
        with httpx.stream("GET", url, timeout=15, follow_redirects=False,
                          headers={"User-Agent": "PengwinCrew/1.0 product-research"}) as response:
            response.raise_for_status()
            chunks = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > 300_000:
                    break
                chunks.append(chunk)
        page = b"".join(chunks).decode("utf-8", errors="replace")
        visible = _clean_text(page, 300_000)
        range_match = re.search(
            r"(?:cost between|price range|from)?\s*\$\s*(\d{1,3}(?:\.\d{2})?)"
            r"\s*(?:-|–|—|to|and)\s*\$\s*(\d{1,3}(?:\.\d{2})?)",
            visible, re.I,
        )
        if not range_match:
            return PublisherFact(status="unavailable", source_url=url,
                                 checked_at=datetime.now(timezone.utc),
                                 note="The official page did not expose a readable price range")
        low, high = (float(part) for part in range_match.groups())
        if low <= 0 or high < low:
            raise ValueError("Invalid product price range")
        return PublisherFact(
            status="ok", source_url=url, checked_at=datetime.now(timezone.utc),
            currency="USD", min_price=low, max_price=high,
            note="Official catalog product range; shipping, tax, printing choices and stock require a current quote.",
        )
    except (httpx.HTTPError, ValueError):
        return PublisherFact(status="error", source_url=url,
                             note="The official product page could not be checked")
