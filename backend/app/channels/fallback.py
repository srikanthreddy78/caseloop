"""Pick where a reply actually goes: a case's channel, unless that channel is known to be failing."""

from __future__ import annotations

from app.models import Business, Case, Channel, Customer, Direction


def delivery_channel(case: Case, customer: Customer, business: Business | None = None) -> str:
    """SMS that the carrier keeps rejecting falls back to email when we know the customer's address.
    A business can also prefer email outright for phone-originated cases (until SMS is registered)."""
    if case.channel in (Channel.SMS, Channel.VOICE) and customer.email:
        prefer_email = (
            bool((business.settings or {}).get("prefer_email_over_sms")) if business else False
        )
        if prefer_email:
            return Channel.EMAIL
    if case.channel == Channel.SMS and customer.email:
        sms_failed = any(
            m.direction == Direction.OUTBOUND
            and m.channel == Channel.SMS
            and (m.meta or {}).get("delivery") == "failed"
            for m in case.messages
        )
        if sms_failed:
            return Channel.EMAIL
    return case.channel
