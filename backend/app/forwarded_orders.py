"""Order emails forwarded to orders@<inbound domain>: who may send them, and how they are stored.

Anyone who knows the forward address could otherwise plant orders the agent would trust, so a
forwarded email is imported only when its sender is on the business's ``order_email_senders``
list (addresses or domains such as ``orders@shopify.com`` or ``amazon.com``), or when Resend
reports a DKIM pass for a listed domain.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business
from app.orders.extract import OrderExtractor
from app.orders.store import upsert_orders

log = logging.getLogger(__name__)

FORWARDED_NOTE = "imported from a forwarded email"


def order_email_senders(business: Business) -> list[str]:
    return [str(s).lower() for s in (business.settings or {}).get("order_email_senders") or []]


def _domain_listed(domain: str, allowed: list[str]) -> bool:
    return any(
        "@" not in entry and (domain == entry or domain.endswith("." + entry)) for entry in allowed
    )


def _dkim_passed(authentication: Any, sender_domain: str, allowed: list[str]) -> bool:
    """Resend reports ``{"dkim": "pass"}`` once the signing domain matches the From domain;
    tolerate a richer shape that names the signing domain explicitly."""
    if not isinstance(authentication, dict):
        return False
    dkim = authentication.get("dkim")
    results = dkim if isinstance(dkim, list) else [dkim]
    for item in results:
        if isinstance(item, dict):
            result = item.get("result") or item.get("status")
            domain = str(item.get("domain") or item.get("signing_domain") or sender_domain).lower()
        else:
            result, domain = item, sender_domain
        if str(result).lower() == "pass" and _domain_listed(domain, allowed):
            return True
    return False


def sender_allowed(business: Business, sender: str, authentication: Any = None) -> bool:
    allowed = order_email_senders(business)
    sender = sender.strip().lower()
    if not allowed or not sender or "@" not in sender:
        return False
    domain = sender.rsplit("@", 1)[1]
    if sender in allowed or _domain_listed(domain, allowed):
        return True
    return _dkim_passed(authentication, domain, allowed)


async def import_forwarded_orders(
    session: AsyncSession,
    business: Business,
    text: str,
    *,
    extractor: OrderExtractor,
    now: datetime,
) -> int:
    """Extract and store the orders in a trusted forwarded email, marked as such for the agent."""
    records = await extractor.extract(text, source="email_forward")
    if not records:
        return 0
    for rec in records:
        rec.notes = FORWARDED_NOTE if not rec.notes else f"{FORWARDED_NOTE}; {rec.notes}"
    return await upsert_orders(session, business.id, records, now=now, raw={"via": "email_forward"})
