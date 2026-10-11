"""Job: pull a finished call's transcript from Dial and hand it to the agent."""

from __future__ import annotations

import logging

from sqlalchemy import select

from app.cases.service import (
    append_inbound,
    enqueue_job,
    find_case_for_customer,
    get_or_create_customer,
    load_business,
    open_case,
)
from app.channels.dial import transcript_text
from app.models import Business, Channel, JobKind, Message, Sender
from app.scheduler import JobContext

log = logging.getLogger(__name__)


async def handle_fetch_call_transcript(ctx: JobContext) -> None:
    s = ctx.services
    client = getattr(s.channels, "dial", None)
    if client is None:
        log.warning("fetch_call_transcript skipped: Dial client not configured")
        return
    call_id = str(ctx.job.payload.get("call_id", ""))
    call = await client.get_call(call_id)
    text = transcript_text(call).strip()
    if not text:
        raise RuntimeError(f"call {call_id} has no transcript yet")  # retried by the scheduler
    now = ctx.clock.now()
    async with ctx.session_factory() as session:
        dup = await session.execute(
            select(Message.id).where(Message.provider_message_id == call_id).limit(1)
        )
        if dup.first() is not None:
            return
        business = await load_business(session, ctx.job.business_id)
        assert business is not None
        direction = getattr(call, "direction", "outbound")
        customer_number = (
            getattr(call, "to", None) if direction == "outbound" else getattr(call, "from_", None)
        )
        if not customer_number:
            log.warning("call %s has no customer number", call_id)
            return
        customer = await get_or_create_customer(session, business, phone=customer_number)
        case, _ = await find_case_for_customer(
            session, business, customer, now=now, reopen_window_days=s.settings.reopen_window_days
        )
        if case is None:
            case = await open_case(
                session, business, customer, channel=Channel.SMS, now=now, subject="Phone call"
            )
        await append_inbound(
            session,
            business,
            case,
            text=f"[Phone call transcript]\n{text}",
            now=now,
            channel=Channel.VOICE,
            provider_message_id=call_id,
            meta={
                "source": "dial_call",
                "call_id": call_id,
                "duration": getattr(call, "duration", None),
                "status": getattr(call, "status", None),
            },
            schedule_turn=False,
        )
        await enqueue_job(
            session,
            business_id=business.id,
            kind=JobKind.AGENT_TURN,
            case_id=case.id,
            due_at=now,
            payload={"trigger": "call_transcribed"},
        )
        await session.commit()


def _unused(_: Business, __: Sender) -> None:  # keeps imports explicit for type checkers
    return None
