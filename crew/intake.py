"""Deterministic boundary around model-extracted Slack purchase requests."""

from __future__ import annotations

import re

from .catalog import PRODUCTS
from .inference import ConciergePlan


ITEM_WORDS = {
    "OAT-MILK": ("oat milk", "oatmilk"),
    "COFFEE": ("coffee",),
    "HOODIE-BER": ("hoodie", "hoodies", "sweatshirt", "sweatshirts"),
    "HOODIE-SF": ("hoodie", "hoodies", "sweatshirt", "sweatshirts"),
    "WELCOME-KIT": ("welcome kit", "onboarding kit"),
}
NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "dozen": 12, "fifteen": 15,
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
}


def _mentioned_numbers(text: str) -> set[int]:
    found = {int(value) for value in re.findall(r"\b\d+\b", text)}
    words = set(re.findall(r"[a-z]+", text.lower()))
    found.update(value for word, value in NUMBER_WORDS.items() if word in words)
    if re.search(r"\b(?:a|an)\s+(?:new\s+)?(?:welcome|onboarding)\s+kit\b", text.lower()):
        found.add(1)
    return found


def checked_plan(plan: ConciergePlan, request_text: str) -> ConciergePlan:
    """Reject unsupported or invented line items before creating any jobs."""
    if plan.clarification:
        if plan.items or plan.lunch_headcount is not None:
            raise ValueError("A clarification cannot also place an order")
        return plan
    if not plan.items and plan.lunch_headcount is None:
        raise ValueError("No supported action or clarification")
    lower = request_text.lower()
    numbers = _mentioned_numbers(lower)
    seen: set[str] = set()
    for item in plan.items:
        if item.sku in seen:
            raise ValueError("Duplicate catalog item")
        seen.add(item.sku)
        if not any(word in lower for word in ITEM_WORDS[item.sku]):
            raise ValueError("Item was not named by the employee")
        office_words = ("berlin", "ber") if item.office == "BER" else ("san francisco", "sf")
        if not any(re.search(rf"\b{re.escape(word)}\b", lower) for word in office_words):
            raise ValueError("Office was not named by the employee")
        product = PRODUCTS[item.sku]
        expected_office = "BER" if product.currency == "EUR" else "SF"
        if item.office != expected_office:
            raise ValueError("Catalog item is not available for that office")
        if item.quantity not in numbers:
            raise ValueError("Quantity was not stated by the employee")
    if plan.lunch_headcount is not None:
        if not re.search(r"\blunch\b", lower) or not re.search(r"\bberlin\b|\bber\b", lower):
            raise ValueError("Berlin lunch was not requested")
        if plan.lunch_headcount not in numbers:
            raise ValueError("Lunch headcount was not stated")
    return plan
