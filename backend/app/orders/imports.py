"""Bulk intake: CSV/JSON uploads and API pushes become stored orders."""

from __future__ import annotations

import csv
import io
from typing import Any

from app.orders.base import OrderItem, OrderRecord, Tracking, normalize_order_number
from app.orders.providers import normalize_status

DEFAULT_COLUMNS: dict[str, tuple[str, ...]] = {
    "order_number": (
        "order_number",
        "order",
        "order id",
        "order_id",
        "number",
        "name",
        "amazon-order-id",
        "order-id",
    ),
    "status": ("status", "order status", "fulfillment_status", "order-status"),
    "customer_email": ("email", "customer_email", "buyer-email", "customer email"),
    "customer_phone": ("phone", "customer_phone", "buyer-phone-number"),
    "customer_name": ("customer", "customer_name", "name", "buyer-name", "recipient-name"),
    "total": ("total", "amount", "order_total", "item-price"),
    "currency": ("currency", "currency_code"),
    "ordered_at": ("ordered_at", "created_at", "date", "order date", "purchase-date"),
    "shipped_at": ("shipped_at", "ship date", "ship-date"),
    "delivered_at": ("delivered_at", "delivered", "delivery date"),
    "tracking_number": ("tracking", "tracking_number", "tracking number"),
    "carrier": ("carrier", "ship-service-level"),
    "item_name": ("item", "product", "product-name", "title", "sku"),
    "quantity": ("quantity", "qty", "quantity-purchased"),
}


def resolve_mapping(headers: list[str], mapping: dict[str, str] | None) -> dict[str, str]:
    """Return {our_field: column_name} using the explicit mapping first, then common names."""
    lowered = {h.lower().strip(): h for h in headers}
    out: dict[str, str] = {}
    for field, candidates in DEFAULT_COLUMNS.items():
        if mapping and mapping.get(field) and mapping[field] in headers:
            out[field] = mapping[field]
            continue
        for c in candidates:
            if c in lowered:
                out[field] = lowered[c]
                break
    return out


def rows_to_records(
    rows: list[dict[str, Any]], *, source: str, mapping: dict[str, str] | None = None
) -> list[OrderRecord]:
    if not rows:
        return []
    headers = list(rows[0].keys())
    m = resolve_mapping(headers, mapping)
    if "order_number" not in m:
        raise ValueError("could not find an order number column; map it explicitly")
    by_number: dict[str, OrderRecord] = {}
    for row in rows:

        def g(f: str, row: dict[str, Any] = row) -> str | None:
            if f not in m or row.get(m[f]) in (None, ""):
                return None
            return str(row.get(m[f])).strip()

        number = g("order_number")
        if not number:
            continue
        number = normalize_order_number(number)
        tracking_number = g("tracking_number")
        total = g("total")
        rec = by_number.get(number)
        if rec is None:
            rec = OrderRecord(
                source=source,
                source_order_id=number,
                order_number=number,
                status=normalize_status(g("status")),
                customer_email=(g("customer_email") or "").lower() or None,
                customer_phone=g("customer_phone"),
                customer_name=g("customer_name"),
                total=float(total.replace(",", "")) if total else None,
                currency=g("currency"),
                ordered_at=g("ordered_at"),
                shipped_at=g("shipped_at"),
                delivered_at=g("delivered_at"),
                tracking=Tracking(number=tracking_number, carrier=g("carrier"))
                if tracking_number
                else None,
            )
            by_number[number] = rec
        item = g("item_name")
        if item:
            qty = g("quantity")
            rec.items.append(OrderItem(name=item, quantity=int(float(qty)) if qty else 1))
    return list(by_number.values())


def parse_csv(text: str) -> list[dict[str, Any]]:
    reader = csv.DictReader(io.StringIO(text.lstrip("\ufeff")), strict=True)
    rows = []
    try:
        for row in reader:
            if None in row:
                raise ValueError(f"CSV row {reader.line_num} has more values than column headers")
            rows.append(dict(row))
    except csv.Error as exc:
        raise ValueError(f"invalid CSV: {exc}") from exc
    return rows
