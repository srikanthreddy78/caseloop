"""Orders from anywhere: one normalized record, many sources, the source always labeled."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

SOURCE_LABELS: dict[str, str] = {
    "shopify": "Shopify",
    "amazon": "Amazon",
    "woocommerce": "WooCommerce",
    "custom_api": "Your website API",
    "tracking_page": "Tracking page",
    "email_forward": "Forwarded email",
    "csv": "CSV import",
    "api": "Pushed via API",
    "fake": "Test data",
}


def source_label(source: str) -> str:
    return SOURCE_LABELS.get(source, source.replace("_", " ").title())


class OrderItem(BaseModel):
    name: str
    quantity: int = 1
    price: float | None = None
    sku: str | None = None


class Tracking(BaseModel):
    carrier: str | None = None
    number: str | None = None
    url: str | None = None
    status: str | None = None
    last_update: str | None = None


class OrderRecord(BaseModel):
    """Provider-agnostic order. Also the structured-output schema for LLM extraction."""

    source: str
    source_order_id: str
    order_number: str
    status: str = Field(
        description="One of: placed, processing, shipped, delivered, cancelled, refunded, returned, unknown"
    )
    customer_email: str | None = None
    customer_phone: str | None = None
    customer_name: str | None = None
    items: list[OrderItem] = Field(default_factory=list)
    total: float | None = None
    currency: str | None = None
    ordered_at: str | None = Field(default=None, description="ISO 8601 date or datetime")
    shipped_at: str | None = None
    delivered_at: str | None = None
    tracking: Tracking | None = None
    shipping_address: str | None = None
    notes: str | None = None

    @property
    def source_label(self) -> str:
        return source_label(self.source)

    def summary(self) -> dict[str, Any]:
        """What the agent sees: source first, then only decision-relevant fields. Never the
        customer's contact details: the agent already knows who it is talking to."""
        return {
            "source": self.source_label,
            "order_number": self.order_number,
            "status": self.status,
            "ordered_at": self.ordered_at,
            "shipped_at": self.shipped_at,
            "delivered_at": self.delivered_at,
            "total": self.total,
            "currency": self.currency,
            "items": [{"name": i.name, "quantity": i.quantity} for i in self.items][:10],
            "tracking": self.tracking.model_dump() if self.tracking else None,
            "notes": self.notes,
        }

    def unverified_summary(self) -> dict[str, Any]:
        """What the agent sees for an order whose contact details do not match the customer."""
        return {
            "order_number": self.order_number,
            "status": "found_but_unverified",
            "verify": "ask the customer to confirm the email or phone used on the order",
        }

    def belongs_to(self, email: str | None, phone: str | None) -> bool:
        """True unless the order carries an email or phone and none of them is the customer's.

        An order with no contact details on it (a bare CSV, a tracking page) cannot contradict
        the customer, so it passes; one that names someone else does not.
        """
        checks: list[bool] = []
        if self.customer_email:
            checks.append(bool(email) and _norm_email(self.customer_email) == _norm_email(email))
        if self.customer_phone:
            checks.append(bool(phone) and _same_phone(self.customer_phone, phone))
        return not checks or any(checks)


class OrderQuery(BaseModel):
    order_number: str | None = None
    email: str | None = None
    phone: str | None = None

    def is_empty(self) -> bool:
        return not (self.order_number or self.email or self.phone)


class OrderProvider(Protocol):
    """A live source we can ask at lookup time. Stored sources (CSV, API push, forwarded email)
    live in the database and are searched by the store, not through a provider."""

    source: str

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]: ...


class ProviderNotConfigured(RuntimeError):
    """Raised when a provider exists in settings but is missing credentials; surfaced to the agent."""


def normalize_order_number(value: str) -> str:
    return value.strip().lstrip("#").upper()


def _norm_email(value: str | None) -> str:
    return (value or "").strip().lower()


def _digits(value: str | None) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


def _same_phone(a: str, b: str | None) -> bool:
    """Digits-only comparison; a national number matches its international form."""
    x, y = _digits(a), _digits(b)
    if not x or not y:
        return False
    if x == y:
        return True
    short, long_ = sorted((x, y), key=len)
    return len(short) >= 7 and long_.endswith(short)


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
