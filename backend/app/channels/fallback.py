"""Pick where a reply actually goes: a case's channel, unless that channel is known to be failing."""

from __future__ import annotations

from app.models import Case, Channel, Customer, Direction


def delivery_channel(case: Case, customer: Customer) -> str:
    """SMS that the carrier keeps rejecting falls back to email when we know the customer's address."""
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
