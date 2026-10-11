"""Live order sources. Each one is configured per business in business.settings["order_sources"]."""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import quote

import httpx

from app.channels.email import html_to_text
from app.orders.base import (
    OrderItem,
    OrderQuery,
    OrderRecord,
    ProviderNotConfigured,
    Tracking,
    normalize_order_number,
)
from app.orders.extract import OrderExtractor

log = logging.getLogger(__name__)

HTTP_TIMEOUT = 15.0


class FakeOrderProvider:
    source = "fake"

    def __init__(self, records: list[OrderRecord] | None = None) -> None:
        self.records = records or []
        self.queries: list[OrderQuery] = []

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]:
        self.queries.append(query)
        out = []
        for r in self.records:
            same_number = bool(query.order_number) and normalize_order_number(
                query.order_number or ""
            ) == normalize_order_number(r.order_number)
            same_email = bool(query.email and r.customer_email) and (
                (query.email or "").lower() == (r.customer_email or "").lower()
            )
            if same_number or same_email:
                out.append(r)
        return out


def _dig(data: Any, path: str) -> Any:
    """Dotted path with list indexes: `fulfillments.0.tracking_number`."""
    cur = data
    for part in path.split("."):
        if cur is None:
            return None
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(cur, dict):
            cur = cur.get(part)
        else:
            return None
    return cur


STATUS_ALIASES = {
    "fulfilled": "delivered",
    "complete": "delivered",
    "completed": "delivered",
    "delivered": "delivered",
    "shipped": "shipped",
    "in_transit": "shipped",
    "in transit": "shipped",
    "partial": "shipped",
    "unfulfilled": "processing",
    "processing": "processing",
    "pending": "placed",
    "paid": "placed",
    "open": "placed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "refunded": "refunded",
    "returned": "returned",
}


def normalize_status(value: Any) -> str:
    if value is None:
        return "unknown"
    key = str(value).strip().lower()
    return STATUS_ALIASES.get(
        key,
        key
        if key
        in {"placed", "processing", "shipped", "delivered", "cancelled", "refunded", "returned"}
        else "unknown",
    )


class CustomApiProvider:
    """The business's own JSON endpoint. Config:
    {"type": "custom_api", "url_template": "https://x/orders/{order_number}", "headers": {...},
     "list_path": "data" (optional, when the response wraps a list),
     "mapping": {"order_number": "id", "status": "state", "delivered_at": "delivered_on",
                 "tracking_number": "shipment.tracking", "tracking_url": "...", "total": "amount",
                 "customer_email": "email", "items": "line_items", "item_name": "title", "item_qty": "qty"}}
    """

    source = "custom_api"

    def __init__(self, config: dict[str, Any]) -> None:
        self.url_template = str(config.get("url_template") or "")
        self.email_url_template = config.get("email_url_template")
        self.headers = dict(config.get("headers") or {})
        self.list_path = config.get("list_path")
        self.mapping = dict(config.get("mapping") or {})
        if not self.url_template:
            raise ProviderNotConfigured("custom_api needs a url_template containing {order_number}")

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]:
        url: str | None = None
        if query.order_number and "{order_number}" in self.url_template:
            url = self.url_template.replace(
                "{order_number}", quote(normalize_order_number(query.order_number))
            )
        elif query.email and self.email_url_template:
            url = str(self.email_url_template).replace("{email}", quote(query.email))
        if not url:
            return []
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as http:
            resp = await http.get(url, headers=self.headers)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        rows = _dig(data, self.list_path) if self.list_path else data
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            return []
        return [r for r in (self._map(row) for row in rows) if r is not None]

    def _map(self, row: dict[str, Any]) -> OrderRecord | None:
        m = self.mapping

        def get(key: str, default: Any = None) -> Any:
            return _dig(row, m[key]) if key in m else default

        number = get("order_number") or row.get("order_number") or row.get("id") or row.get("name")
        if number is None:
            return None
        items_raw = get("items") or row.get("items") or row.get("line_items") or []
        items = []
        for it in items_raw if isinstance(items_raw, list) else []:
            if isinstance(it, dict):
                items.append(
                    OrderItem(
                        name=str(
                            _dig(it, m.get("item_name", "name")) or _dig(it, "title") or "item"
                        ),
                        quantity=int(_dig(it, m.get("item_qty", "quantity")) or 1),
                    )
                )
        tracking_number = get("tracking_number")
        tracking = (
            Tracking(number=str(tracking_number), url=get("tracking_url"), carrier=get("carrier"))
            if tracking_number
            else None
        )
        total = get("total") or row.get("total")
        return OrderRecord(
            source=self.source,
            source_order_id=str(get("source_order_id") or row.get("id") or number),
            order_number=normalize_order_number(str(number)),
            status=normalize_status(get("status") or row.get("status")),
            customer_email=get("customer_email") or row.get("email"),
            customer_name=get("customer_name"),
            items=items,
            total=float(str(total)) if total not in (None, "") else None,
            currency=get("currency") or row.get("currency"),
            ordered_at=get("ordered_at") or row.get("created_at"),
            shipped_at=get("shipped_at"),
            delivered_at=get("delivered_at"),
            tracking=tracking,
        )


class TrackingPageProvider:
    """The business only has a public tracking page: fetch it and let the extractor read it.
    Config: {"type": "tracking_page", "url_template": "https://x/track?order={order_number}"}"""

    source = "tracking_page"

    def __init__(self, config: dict[str, Any], extractor: OrderExtractor) -> None:
        self.url_template = str(config.get("url_template") or "")
        self.extractor = extractor
        if "{order_number}" not in self.url_template:
            raise ProviderNotConfigured(
                "tracking_page needs a url_template containing {order_number}"
            )

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]:
        if not query.order_number:
            return []
        number = normalize_order_number(query.order_number)
        url = self.url_template.replace("{order_number}", quote(number))
        async with httpx.AsyncClient(
            timeout=HTTP_TIMEOUT, follow_redirects=True, headers={"User-Agent": "Caseloop/1.0"}
        ) as http:
            resp = await http.get(url)
        if resp.status_code >= 400:
            return []
        text = (
            html_to_text(resp.text) if "html" in resp.headers.get("content-type", "") else resp.text
        )
        records = await self.extractor.extract(text, source=self.source, hint=number)
        return [r for r in records if normalize_order_number(r.order_number) == number] or records[
            :1
        ]


class ShopifyProvider:
    """Shopify Admin REST API. Config: {"type": "shopify", "shop": "acme.myshopify.com", "token": "shpat_..."}"""

    source = "shopify"

    def __init__(self, config: dict[str, Any]) -> None:
        self.shop = str(config.get("shop") or "").replace("https://", "").strip("/")
        self.token = str(config.get("token") or "")
        self.version = str(config.get("api_version") or "2025-10")
        if not self.shop or not self.token:
            raise ProviderNotConfigured(
                "Shopify needs the shop domain and an Admin API access token"
            )

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]:
        params: dict[str, str] = {"status": "any", "limit": "10"}
        if query.order_number:
            params["name"] = "#" + normalize_order_number(query.order_number).lstrip("#")
        elif query.email:
            params["email"] = query.email
        else:
            return []
        url = f"https://{self.shop}/admin/api/{self.version}/orders.json"
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
            resp = await http.get(
                url, params=params, headers={"X-Shopify-Access-Token": self.token}
            )
        resp.raise_for_status()
        return [self._map(o) for o in resp.json().get("orders", [])]

    def _map(self, o: dict[str, Any]) -> OrderRecord:
        fulfillments = o.get("fulfillments") or []
        tracking = None
        shipped_at = delivered_at = None
        if fulfillments:
            f = fulfillments[-1]
            tracking = Tracking(
                carrier=f.get("tracking_company"),
                number=f.get("tracking_number"),
                url=f.get("tracking_url"),
                status=f.get("shipment_status"),
                last_update=f.get("updated_at"),
            )
            shipped_at = f.get("created_at")
            if f.get("shipment_status") == "delivered":
                delivered_at = f.get("updated_at")
        status = (
            "cancelled"
            if o.get("cancelled_at")
            else normalize_status(
                "delivered"
                if delivered_at
                else (o.get("fulfillment_status") or o.get("financial_status"))
            )
        )
        if o.get("financial_status") == "refunded":
            status = "refunded"
        customer = o.get("customer") or {}
        addr = o.get("shipping_address") or {}
        return OrderRecord(
            source=self.source,
            source_order_id=str(o.get("id")),
            order_number=normalize_order_number(str(o.get("name") or o.get("order_number"))),
            status=status,
            customer_email=o.get("email") or customer.get("email"),
            customer_phone=o.get("phone") or customer.get("phone"),
            customer_name=" ".join(
                x for x in (customer.get("first_name"), customer.get("last_name")) if x
            )
            or None,
            items=[
                OrderItem(
                    name=li.get("title", "item"),
                    quantity=int(li.get("quantity", 1)),
                    price=float(li.get("price", 0) or 0),
                    sku=li.get("sku"),
                )
                for li in o.get("line_items", [])
            ],
            total=float(o.get("total_price") or 0) or None,
            currency=o.get("currency"),
            ordered_at=o.get("created_at"),
            shipped_at=shipped_at,
            delivered_at=delivered_at,
            tracking=tracking,
            shipping_address=", ".join(
                str(addr.get(k))
                for k in ("address1", "city", "province", "zip", "country")
                if addr.get(k)
            )
            or None,
        )


class AmazonProvider:
    """Amazon Selling Partner API. Needs LWA client id/secret, refresh token, marketplace id.
    Config: {"type": "amazon", "client_id": ..., "client_secret": ..., "refresh_token": ..., "marketplace_id": ...}"""

    source = "amazon"

    def __init__(self, config: dict[str, Any]) -> None:
        missing = [
            k
            for k in ("client_id", "client_secret", "refresh_token", "marketplace_id")
            if not config.get(k)
        ]
        if missing:
            raise ProviderNotConfigured(
                "Amazon needs Selling Partner API credentials: " + ", ".join(missing)
            )
        self.config = config

    async def lookup(self, query: OrderQuery) -> list[OrderRecord]:
        raise ProviderNotConfigured(
            "Amazon Selling Partner API lookup is not wired yet; import Amazon orders by CSV or forwarded "
            "emails meanwhile"
        )


_AMAZON_ORDER = re.compile(r"\b\d{3}-\d{7}-\d{7}\b")


def looks_like_amazon_order(number: str) -> bool:
    return bool(_AMAZON_ORDER.fullmatch(number.strip()))
