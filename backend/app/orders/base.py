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
        """What the agent sees: source first, then only decision-relevant fields."""
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
            "customer_email": self.customer_email,
            "customer_name": self.customer_name,
            "notes": self.notes,
        }


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


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
