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
from app.orders.store import row_to_record, search_orders, upsert_orders

log = logging.getLogger(__name__)

LIVE_TYPES = ("shopify", "amazon", "custom_api", "tracking_page")
STORED_TYPES = ("csv", "api", "email_forward")
SECRET_KEYS = ("token", "client_secret", "refresh_token", "headers")


@dataclass
class LookupResult:
    orders: list[OrderRecord] = field(default_factory=list)
    sources_checked: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "found": [o.summary() for o in self.orders],
            "sources_checked": [source_label(s) for s in self.sources_checked],
            "errors": self.errors,
            "no_order_system_connected": not self.sources_checked,
        }


class OrderService:
    def __init__(
        self, extractor: OrderExtractor, extra_providers: list[OrderProvider] | None = None
    ) -> None:
        self.extractor = extractor
        self.extra_providers = extra_providers or []

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
                    out.append(CustomApiProvider(cfg))
                elif kind == "tracking_page":
                    out.append(TrackingPageProvider(cfg, self.extractor))
            except ProviderNotConfigured as exc:
                out.append(exc)
        return out

    def configured_sources(self, business: Business) -> list[dict[str, Any]]:
        """For the dashboard: every configured source with secrets masked."""
        out = []
        for cfg in business.settings.get("order_sources", []) or []:
            masked = {k: ("••••" if k in SECRET_KEYS and v else v) for k, v in cfg.items()}
            masked["label"] = source_label(str(cfg.get("type", "")))
            out.append(masked)
        return out

    async def lookup(
        self, session: AsyncSession, business: Business, query: OrderQuery, *, now: datetime
    ) -> LookupResult:
        result = LookupResult()
        if query.is_empty():
            return result
        seen: set[tuple[str, str]] = set()
        stored = await search_orders(session, business.id, query)
        stored_sources = {row.source for row in stored}
        for row in stored:
            rec = row_to_record(row)
            seen.add((rec.source, rec.source_order_id))
            result.orders.append(rec)
        has_stored = any(
            str(c.get("type")) in STORED_TYPES
            for c in business.settings.get("order_sources", []) or []
        ) or bool(stored_sources & set(STORED_TYPES))
        if has_stored:
            result.sources_checked.extend(sorted(stored_sources & set(STORED_TYPES)) or ["csv"])
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
                for r in fresh:
                    seen.add((r.source, r.source_order_id))
                result.orders = fresh + result.orders  # live data first: most recently synced
        return result
