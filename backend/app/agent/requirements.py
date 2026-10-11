"""Canonical requirement keys. Models name the same thing many ways; the product needs one name."""

from __future__ import annotations

import re

CANONICAL_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "order_number",
        re.compile(
            r"order[_ ]?(number|num|no|id|ref|reference)?$|^order$|purchase[_ ]?(id|number)"
        ),
    ),
    ("photos", re.compile(r"photo|picture|image|pic(s)?$|screenshot")),
    ("shipping_address", re.compile(r"(shipping|delivery|mailing|return)[_ ]?address|^address$")),
    ("delivery_date", re.compile(r"deliver(y|ed)[_ ]?date|date[_ ]?(of[_ ]?)?deliver")),
    ("tracking_number", re.compile(r"tracking")),
    (
        "preferred_remedy",
        re.compile(
            r"remedy|resolution[_ ]?preference|preferred[_ ]?(outcome|option|solution)|refund[_ ]?or[_ ]?replace"
        ),
    ),
    ("email", re.compile(r"^e?mail([_ ]?address)?$|contact[_ ]?email")),
    ("phone", re.compile(r"phone|mobile|telephone")),
    (
        "duplicate_charge_details",
        re.compile(
            r"(duplicate|double|second)[_ ]?(charge|payment)|charge[_ ]?(details|evidence|proof)|statement|receipt"
        ),
    ),
]


def canonical_key(raw: str) -> str:
    """Map a model-chosen key to the canonical one when a rule matches; otherwise slugify it."""
    key = re.sub(r"[^a-z0-9]+", "_", raw.strip().lower()).strip("_")
    for canonical, pattern in CANONICAL_RULES:
        if pattern.search(key):
            return canonical
    return key[:64] or "other"


CANONICAL_LABELS: dict[str, str] = {
    "order_number": "Order number",
    "photos": "Photos",
    "shipping_address": "Shipping address",
    "delivery_date": "Delivery date",
    "tracking_number": "Tracking number",
    "preferred_remedy": "Preferred remedy",
    "email": "Email address",
    "phone": "Phone number",
    "duplicate_charge_details": "Duplicate charge details",
}
