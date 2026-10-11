"""Businesses forward order confirmation / shipping emails to orders@<their inbound domain>."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business
from app.orders.extract import OrderExtractor
from app.orders.store import upsert_orders

FORWARD_LOCAL_PARTS = ("orders", "order")


def forward_address_for(business: Business) -> str:
    domain = business.inbound_email.split("@", 1)[1]
    return f"orders@{domain}"


async def business_for_forward_address(
    session: AsyncSession, recipients: list[str]
) -> Business | None:
    for addr in recipients:
        local, _, domain = addr.lower().partition("@")
        if local.split("+", 1)[0] not in FORWARD_LOCAL_PARTS:
            continue
        stmt = select(Business).where(Business.inbound_email.like(f"%@{domain}"))
        business = (await session.execute(stmt)).scalars().first()
        if business is not None:
            return business
    return None


async def import_forwarded_email(
    session: AsyncSession,
    business: Business,
    text: str,
    *,
    extractor: OrderExtractor,
    now: datetime,
) -> int:
    records = await extractor.extract(text, source="email_forward")
    if not records:
        return 0
    return await upsert_orders(session, business.id, records, now=now, raw={"via": "email_forward"})
