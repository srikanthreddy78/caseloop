"""Answer 'what do we know about this order?' from every connected source, labeled."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business
from app.orders.base import (
    OrderProvider,
    OrderQuery,
    OrderRecord,
    ProviderNotConfigured,
    source_label,
)
from app.orders.extract import OrderExtractor
from app.orders.providers import (
    AmazonProvider,
    CustomApiProvider,
    ShopifyProvider,
    TrackingPageProvider,
)
from app.orders.store import row_to_record, search_orders, source_counts, upsert_orders

log = logging.getLogger(__name__)

LIVE_TYPES = ("shopify", "amazon", "custom_api", "tracking_page")
STORED_TYPES = ("csv", "api", "email_forward")
# Everything else in a source config is treated as a secret and masked on the way out.
NON_SECRET_KEYS = frozenset(
    {
        "id",
        "type",
        "shop",
        "url_template",
        "email_url_template",
        "list_path",
        "mapping",
        "api_version",
        "marketplace_id",
        "label",
    }
)
MASK = "••••"


def mask_source(cfg: dict[str, Any]) -> dict[str, Any]:
    """A source config safe to show: only known non-secret keys keep their value."""
    return {k: (v if k in NON_SECRET_KEYS or not v else MASK) for k, v in cfg.items()}


@dataclass
class LookupResult:
    orders: list[OrderRecord] = field(default_factory=list)
    sources_checked: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Everything found, for the business's own dashboard and API."""
        return self._summary([o.summary() for o in self.orders])

    def customer_summary(self, email: str | None, phone: str | None) -> dict[str, Any]:
        """What the agent may see on behalf of one customer: an order whose contact details
        belong to someone else is reduced to its number and a request to verify."""
        found = [
            o.summary() if o.belongs_to(email, phone) else o.unverified_summary()
            for o in self.orders
        ]
        return self._summary(found)

    def _summary(self, found: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "found": found,
            "sources_checked": [source_label(s) for s in self.sources_checked],
            "errors": self.errors,
            "no_order_system_connected": not self.sources_checked,
        }


class OrderService:
    def __init__(
        self,
        extractor: OrderExtractor,
        extra_providers: list[OrderProvider] | None = None,
        *,
        allow_localhost: bool = False,
    ) -> None:
        self.extractor = extractor
        self.extra_providers = extra_providers or []
        self.allow_localhost = allow_localhost

    def providers_for(self, business: Business) -> list[OrderProvider | ProviderNotConfigured]:
        out: list[OrderProvider | ProviderNotConfigured] = list(self.extra_providers)
        for cfg in business.settings.get("order_sources", []) or []:
            kind = str(cfg.get("type", ""))
            try:
                if kind == "shopify":
                    out.append(ShopifyProvider(cfg))
                elif kind == "amazon":
                    out.append(AmazonProvider(cfg))
                elif kind == "custom_api":
                    out.append(CustomApiProvider(cfg, allow_localhost=self.allow_localhost))
                elif kind == "tracking_page":
                    out.append(
                        TrackingPageProvider(
                            cfg, self.extractor, allow_localhost=self.allow_localhost
                        )
                    )
            except ProviderNotConfigured as exc:
                out.append(exc)
        return out

    def configured_sources(self, business: Business) -> list[dict[str, Any]]:
        """For the dashboard: every configured source with secrets masked."""
        out = []
        for cfg in business.settings.get("order_sources", []) or []:
            masked = mask_source(cfg)
            masked["label"] = source_label(str(cfg.get("type", "")))
            out.append(masked)
        return out

    async def lookup(
        self, session: AsyncSession, business: Business, query: OrderQuery, *, now: datetime
    ) -> LookupResult:
        """Live providers first, then the business's own stored orders (CSV, API push, forwarded
        email). Rows synced from a live provider earlier are only a cache: every live hit is
        written back over them, and they are never served in place of a fresh answer."""
        result = LookupResult()
        if query.is_empty():
            return result
        stored = [
            row_to_record(row)
            for row in await search_orders(session, business.id, query, sources=STORED_TYPES)
        ]
        # Stored orders count as a connected source whenever the business has any of them, even
        # if this particular query matched nothing.
        existing = await source_counts(session, business.id)
        stored_kinds = sorted((set(existing) | {r.source for r in stored}) & set(STORED_TYPES))
        if stored_kinds:
            result.sources_checked.extend(stored_kinds)
        live: list[OrderRecord] = []
        seen: set[tuple[str, str]] = set()
        for provider in self.providers_for(business):
            if isinstance(provider, ProviderNotConfigured):
                result.errors.append({"source": "unconfigured", "error": str(provider)})
                continue
            result.sources_checked.append(provider.source)
            try:
                found = await provider.lookup(query)
            except ProviderNotConfigured as exc:
                result.errors.append({"source": source_label(provider.source), "error": str(exc)})
                continue
            except Exception as exc:
                log.warning("order provider %s failed: %s", provider.source, exc)
                result.errors.append(
                    {"source": source_label(provider.source), "error": str(exc)[:200]}
                )
                continue
            fresh = [r for r in found if (r.source, r.source_order_id) not in seen]
            if fresh:
                await upsert_orders(session, business.id, fresh, now=now)
                seen.update((r.source, r.source_order_id) for r in fresh)
                live.extend(fresh)
        result.orders = live + stored
        return result
