"""Orders from anywhere: push, import, search, configure sources, test a lookup."""

from __future__ import annotations

import asyncio
import re
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.api.deps import BusinessDep, OwnerDep, RateLimitApi, ServicesDep, SessionDep
from app.models import Order
from app.netguard import UnsafeUrlError, assert_public_url
from app.orders.base import OrderQuery, OrderRecord, source_label
from app.orders.imports import parse_csv, rows_to_records
from app.orders.service import LIVE_TYPES, MASK, STORED_TYPES
from app.orders.store import row_to_record, source_counts, upsert_orders
from app.schemas import DataOut

router = APIRouter(prefix="/v1/orders", tags=["orders"], dependencies=[RateLimitApi])

SHOP_RE = re.compile(r"^[a-z0-9-]+\.myshopify\.com$")
# The keys that say *where* a source sends requests. A kept secret never follows a new destination.
DESTINATION_KEYS: dict[str, tuple[str, ...]] = {
    "shopify": ("shop",),
    "custom_api": ("url_template", "email_url_template"),
    "tracking_page": ("url_template", "email_url_template"),
    "amazon": ("marketplace_id",),
}


def _invalid(message: str) -> HTTPException:
    return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, message)


async def _validate_destination(cfg: dict[str, Any], *, allow_localhost: bool) -> None:
    """Normalize and check the destination fields in place; 422 on anything we would not call."""
    kind = cfg["type"]
    if kind == "shopify":
        shop = re.sub(r"^https?://", "", str(cfg.get("shop") or "").strip().lower()).rstrip("/")
        if not SHOP_RE.fullmatch(shop):
            raise _invalid("shop must look like acme.myshopify.com")
        cfg["shop"] = shop
    elif kind in ("custom_api", "tracking_page"):
        for key in DESTINATION_KEYS[kind]:
            if not cfg.get(key):
                cfg.pop(key, None)
                continue
            url = str(cfg[key]).strip()
            try:
                await asyncio.to_thread(assert_public_url, url, allow_localhost=allow_localhost)
            except UnsafeUrlError as exc:
                raise _invalid(f"{key}: {exc}") from exc
            cfg[key] = url
        if "{order_number}" not in str(cfg.get("url_template") or ""):
            raise _invalid("url_template must contain {order_number}")


class OrderOut(BaseModel):
    source: str
    source_label: str
    order_number: str
    status: str
    customer_email: str | None
    customer_name: str | None
    total: float | None
    currency: str | None
    ordered_at: str | None
    delivered_at: str | None
    tracking: dict[str, Any] | None
    items: list[dict[str, Any]]
    synced_at: str | None = None


def _out(rec: OrderRecord, synced_at: str | None = None) -> OrderOut:
    return OrderOut(
        source=rec.source,
        source_label=rec.source_label,
        order_number=rec.order_number,
        status=rec.status,
        customer_email=rec.customer_email,
        customer_name=rec.customer_name,
        total=rec.total,
        currency=rec.currency,
        ordered_at=rec.ordered_at,
        delivered_at=rec.delivered_at,
        tracking=rec.tracking.model_dump() if rec.tracking else None,
        items=[i.model_dump() for i in rec.items],
        synced_at=synced_at,
    )


class PushOrdersIn(BaseModel):
    orders: list[dict[str, Any]] = Field(min_length=1, max_length=1000)
    source: str = Field(default="api", pattern=r"^[a-z_]+$")
    mapping: dict[str, str] | None = None


class ImportCsvIn(BaseModel):
    csv: str = Field(min_length=1, max_length=5_000_000)
    source: str = Field(default="csv", pattern=r"^[a-z_]+$")
    mapping: dict[str, str] | None = None


class ImportOut(BaseModel):
    imported: int
    source: str
    source_label: str
    columns_used: dict[str, str]


class SourcesIn(BaseModel):
    order_sources: list[dict[str, Any]] = Field(max_length=10)


class TestLookupIn(BaseModel):
    order_number: str | None = None
    email: str | None = None


@router.get("", response_model=DataOut[list[OrderOut]])
async def list_orders(
    business: BusinessDep,
    session: SessionDep,
    q: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
) -> DataOut[list[OrderOut]]:
    stmt = select(Order).where(Order.business_id == business.id)
    if q:
        needle = q.strip()
        stmt = stmt.where(
            (Order.order_number == needle.lstrip("#").upper())
            | (Order.customer_email == needle.lower())
            | (Order.customer_phone == needle)
        )
    rows = (
        (await session.execute(stmt.order_by(Order.synced_at.desc()).limit(limit))).scalars().all()
    )
    return DataOut(data=[_out(row_to_record(r), r.synced_at.isoformat()) for r in rows])


@router.get("/sources", response_model=DataOut[dict[str, Any]])
async def get_sources(
    business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[dict[str, Any]]:
    counts = await source_counts(session, business.id)
    return DataOut(
        data={
            "configured": services.orders.configured_sources(business),
            "stored_counts": {k: {"count": v, "label": source_label(k)} for k, v in counts.items()},
            "forward_address": f"orders@{business.inbound_email.split('@', 1)[1]}",
            "live_types": list(LIVE_TYPES),
            "stored_types": list(STORED_TYPES),
        }
    )


@router.put("/sources", response_model=DataOut[list[dict[str, Any]]])
async def put_sources(
    payload: SourcesIn,
    owner: OwnerDep,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
) -> DataOut[list[dict[str, Any]]]:
    """Replace the configured live sources. Owners only.

    Every source carries a stable ``id``. A masked secret ('••••') keeps the stored value of the
    source with the same id, but only while that source still points at the same destination:
    a token must be typed again to follow a new shop or URL.
    """
    previous = {
        str(c["id"]): c for c in (business.settings.get("order_sources", []) or []) if c.get("id")
    }
    used_ids: set[str] = set()
    cleaned: list[dict[str, Any]] = []
    for cfg in payload.order_sources:
        kind = str(cfg.get("type", ""))
        if kind not in LIVE_TYPES:
            raise _invalid(f"unknown source type {kind!r}")
        merged = {k: v for k, v in cfg.items() if k != "label"}
        merged["type"] = kind
        given_id = cfg.get("id")
        old = previous.get(given_id) if isinstance(given_id, str) else None
        if old is not None and given_id in used_ids:
            raise _invalid(f"source id {given_id!r} appears twice")
        merged["id"] = old["id"] if old is not None else str(uuid.uuid4())
        used_ids.add(merged["id"])
        await _validate_destination(merged, allow_localhost=services.settings.is_dev)
        moved = old is None or any(
            old.get(k) != merged.get(k) for k in ("type", *DESTINATION_KEYS[kind])
        )
        for key, value in list(merged.items()):
            if value != MASK:
                continue
            if old is not None and key in old and not moved:
                merged[key] = old[key]
            else:
                raise _invalid(
                    f"{key} must be entered again"
                    + (": the destination changed" if old is not None else "")
                )
        cleaned.append(merged)
    settings = dict(business.settings or {})
    settings["order_sources"] = cleaned
    business.settings = settings
    await session.commit()
    await session.refresh(business)
    return DataOut(data=services.orders.configured_sources(business))


@router.post("/sources/test", response_model=DataOut[dict[str, Any]])
async def test_lookup(
    payload: TestLookupIn, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[dict[str, Any]]:
    query = OrderQuery(order_number=payload.order_number, email=payload.email)
    if query.is_empty():
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "give an order_number or email")
    result = await services.orders.lookup(session, business, query, now=services.clock.now())
    await session.commit()
    return DataOut(data=result.summary())


@router.post("", response_model=DataOut[ImportOut], status_code=status.HTTP_201_CREATED)
async def push_orders(
    payload: PushOrdersIn, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[ImportOut]:
    """Any system can push orders as JSON rows; columns are matched by common names or an explicit mapping."""
    try:
        records = rows_to_records(payload.orders, source=payload.source, mapping=payload.mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    n = await upsert_orders(
        session, business.id, records, now=services.clock.now(), raw={"via": "api"}
    )
    await session.commit()
    from app.orders.imports import resolve_mapping

    used = resolve_mapping(list(payload.orders[0].keys()), payload.mapping)
    return DataOut(
        data=ImportOut(
            imported=n,
            source=payload.source,
            source_label=source_label(payload.source),
            columns_used=used,
        )
    )


@router.post("/import", response_model=DataOut[ImportOut], status_code=status.HTTP_201_CREATED)
async def import_csv(
    payload: ImportCsvIn, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[ImportOut]:
    rows = parse_csv(payload.csv)
    if not rows:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "CSV has no data rows")
    try:
        records = rows_to_records(rows, source=payload.source, mapping=payload.mapping)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    n = await upsert_orders(
        session, business.id, records, now=services.clock.now(), raw={"via": "csv"}
    )
    await session.commit()
    from app.orders.imports import resolve_mapping

    used = resolve_mapping(list(rows[0].keys()), payload.mapping)
    return DataOut(
        data=ImportOut(
            imported=n,
            source=payload.source,
            source_label=source_label(payload.source),
            columns_used=used,
        )
    )
