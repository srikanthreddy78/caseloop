"""Orders table: upsert normalized records and search them. The DB is the first source consulted."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Order
from app.orders.base import (
    OrderItem,
    OrderQuery,
    OrderRecord,
    Tracking,
    normalize_order_number,
    parse_dt,
)


def record_to_row(business_id: uuid.UUID, rec: OrderRecord, now: datetime) -> dict[str, Any]:
    return {
        "business_id": business_id,
        "source": rec.source,
        "source_order_id": rec.source_order_id[:200],
        "order_number": normalize_order_number(rec.order_number)[:100],
        "status": (rec.status or "unknown")[:32],
        "customer_email": rec.customer_email.lower()[:320] if rec.customer_email else None,
        "customer_phone": rec.customer_phone[:32] if rec.customer_phone else None,
        "customer_name": rec.customer_name[:200] if rec.customer_name else None,
        "items": [i.model_dump() for i in rec.items],
        "total": rec.total,
        "currency": rec.currency,
        "ordered_at": parse_dt(rec.ordered_at),
        "shipped_at": parse_dt(rec.shipped_at),
        "delivered_at": parse_dt(rec.delivered_at),
        "tracking": rec.tracking.model_dump() if rec.tracking else None,
        "shipping_address": rec.shipping_address,
        "notes": rec.notes,
        "synced_at": now,
    }


def row_to_record(row: Order) -> OrderRecord:
    return OrderRecord(
        source=row.source,
        source_order_id=row.source_order_id,
        order_number=row.order_number,
        status=row.status,
        customer_email=row.customer_email,
        customer_phone=row.customer_phone,
        customer_name=row.customer_name,
        items=[OrderItem(**i) for i in (row.items or [])],
        total=float(row.total) if row.total is not None else None,
        currency=row.currency,
        ordered_at=row.ordered_at.isoformat() if row.ordered_at else None,
        shipped_at=row.shipped_at.isoformat() if row.shipped_at else None,
        delivered_at=row.delivered_at.isoformat() if row.delivered_at else None,
        tracking=Tracking(**row.tracking) if row.tracking else None,
        shipping_address=row.shipping_address,
        notes=row.notes,
    )


async def upsert_orders(
    session: AsyncSession,
    business_id: uuid.UUID,
    records: list[OrderRecord],
    *,
    now: datetime,
    raw: dict[str, Any] | None = None,
) -> int:
    count = 0
    for rec in records:
        values = record_to_row(business_id, rec, now)
        values["raw"] = raw or {}
        stmt = insert(Order).values(**values)
        update_cols = {
            k: v for k, v in values.items() if k not in ("business_id", "source", "source_order_id")
        }
        stmt = stmt.on_conflict_do_update(constraint="uq_order_source_id", set_=update_cols)
        await session.execute(stmt)
        count += 1
    await session.flush()
    return count


async def search_orders(
    session: AsyncSession,
    business_id: uuid.UUID,
    query: OrderQuery,
    limit: int = 10,
    *,
    sources: tuple[str, ...] | None = None,
) -> list[Order]:
    """Stored rows matching the query. ``sources`` restricts which source kinds are returned:
    rows synced from a live provider are its cache, not an answer in their own right."""
    if query.is_empty():
        return []
    conds = []
    if query.order_number:
        conds.append(Order.order_number == normalize_order_number(query.order_number))
    if query.email:
        conds.append(Order.customer_email == query.email.lower().strip())
    if query.phone:
        conds.append(Order.customer_phone == query.phone.strip())
    stmt = select(Order).where(Order.business_id == business_id, or_(*conds))
    if sources is not None:
        stmt = stmt.where(Order.source.in_(sources))
    stmt = stmt.order_by(Order.synced_at.desc()).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


async def source_counts(session: AsyncSession, business_id: uuid.UUID) -> dict[str, int]:
    from sqlalchemy import func

    rows = await session.execute(
        select(Order.source, func.count())
        .where(Order.business_id == business_id)
        .group_by(Order.source)
    )
    return {source: int(n) for source, n in rows.all()}
