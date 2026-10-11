"""Turn free text (a tracking page, a forwarded confirmation email, a CSV row) into OrderRecords."""

from __future__ import annotations

import re
from typing import Any, Protocol, cast

from openai import AsyncOpenAI
from pydantic import BaseModel

from app.orders.base import OrderRecord, Tracking, normalize_order_number

EXTRACT_INSTRUCTIONS = """You extract e-commerce order facts from text. Return every distinct order you can see.
Use the `source` value given by the user message verbatim. Set status to one of: placed, processing,
shipped, delivered, cancelled, refunded, returned, unknown. Dates as ISO 8601. Never invent values:
leave a field null if the text does not state it. source_order_id is the order number when nothing else exists."""


class ExtractedOrders(BaseModel):
    orders: list[OrderRecord]


class OrderExtractor(Protocol):
    name: str

    async def extract(
        self, text: str, *, source: str, hint: str | None = None
    ) -> list[OrderRecord]: ...


class OpenAIOrderExtractor:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    async def extract(
        self, text: str, *, source: str, hint: str | None = None
    ) -> list[OrderRecord]:
        prompt = (
            f"source: {source}\n"
            + (f"looking for order number: {hint}\n" if hint else "")
            + "\n"
            + text[:40000]
        )
        response = await self.client.responses.parse(
            model=self.model,
            instructions=EXTRACT_INSTRUCTIONS,
            input=cast(Any, [{"role": "user", "content": prompt}]),
            text_format=ExtractedOrders,
            reasoning=cast(Any, {"effort": "low"}),
            max_output_tokens=4000,
        )
        parsed = response.output_parsed
        orders = parsed.orders if parsed else []
        for o in orders:
            o.source = source
            o.order_number = normalize_order_number(o.order_number)
            o.source_order_id = o.source_order_id or o.order_number
        return orders


_ORDER = re.compile(
    r"(?:order(?:\s+(?:number|no\.?|id|#))?|#)\s*[:#]?\s*([A-Z]{0,4}-?\d{3,}[A-Z0-9-]*)", re.I
)
_STATUS_WORDS = [
    ("delivered", "delivered"),
    ("out for delivery", "shipped"),
    ("in transit", "shipped"),
    ("shipped", "shipped"),
    ("dispatched", "shipped"),
    ("refunded", "refunded"),
    ("cancelled", "cancelled"),
    ("canceled", "cancelled"),
    ("returned", "returned"),
    ("processing", "processing"),
    ("confirmed", "placed"),
]
_TRACKING = re.compile(r"\b(1Z[0-9A-Z]{16}|\d{20,22}|\d{12,14}|9\d{15,21})\b")
_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")
_TOTAL = re.compile(r"(?:total|amount)\D{0,12}([\d,]+\.\d{2})", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class ScriptedOrderExtractor:
    """Regex heuristics so tests and offline demos work; the model does the real job."""

    name = "scripted"

    async def extract(
        self, text: str, *, source: str, hint: str | None = None
    ) -> list[OrderRecord]:
        m = _ORDER.search(text)
        number = (
            normalize_order_number(m.group(1))
            if m
            else (normalize_order_number(hint) if hint else None)
        )
        if not number:
            return []
        lowered = text.lower()
        status = next((s for w, s in _STATUS_WORDS if w in lowered), "unknown")
        tracking = _TRACKING.search(text)
        dates = _DATE.findall(text)
        total = _TOTAL.search(text)
        email = _EMAIL.search(text)
        return [
            OrderRecord(
                source=source,
                source_order_id=number,
                order_number=number,
                status=status,
                customer_email=email.group(0).lower() if email else None,
                total=float(total.group(1).replace(",", "")) if total else None,
                ordered_at=dates[0] if dates else None,
                delivered_at=dates[-1] if status == "delivered" and dates else None,
                tracking=Tracking(number=tracking.group(1), status=status) if tracking else None,
            )
        ]
