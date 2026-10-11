"""Dial webhook: inbound SMS, call lifecycle, transcripts."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import ServicesDep, SessionDep
from app.cases.service import (
    enqueue_job,
    find_case_for_customer,
    get_or_create_customer,
    ingest_inbound,
    record_event,
)
from app.models import Business, Channel, JobKind, Message, Sender
from app.webhooks.signatures import InvalidSignature, verify_dial

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/dial")
async def dial_webhook(
    request: Request, session: SessionDep, services: ServicesDep
) -> dict[str, str]:
    raw = await request.body()
    secret = services.settings.dial_webhook_secret
    if secret:
        try:
            verify_dial(secret, request.headers.get("x-dial-signature"), raw)
        except InvalidSignature as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"bad signature: {exc}") from exc
    elif not services.settings.is_dev:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "DIAL_WEBHOOK_SECRET is not configured"
        )
    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc

    kind = str(event.get("type", ""))
    data = event.get("data") or {}
    now = services.clock.now()

    if kind == "message.received":
        our_number = data.get("to")
        business = await _business_for_number(session, our_number)
        if business is None:
            return {"status": "ignored", "reason": "unknown number"}
        message_id = data.get("messageId")
        if message_id:
            dup = await session.execute(
                select(Message.id).where(Message.provider_message_id == message_id).limit(1)
            )
            if dup.first() is not None:
                return {"status": "duplicate"}
        body = (data.get("body") or "").strip()
        media = [
            {"filename": m.get("id"), "content_type": m.get("contentType"), "url": m.get("url")}
            for m in (data.get("media") or [])
        ]
        if not body and not media:
            return {"status": "ignored", "reason": "empty"}
        customer = await get_or_create_customer(session, business, phone=data.get("from"))
        result = await ingest_inbound(
            session,
            business,
            customer,
            channel=Channel.SMS,
            text=body or "(media only)",
            provider_message_id=message_id,
            attachments=media,
            now=now,
            reopen_window_days=services.settings.reopen_window_days,
            meta={"source": "dial", "dial_channel": data.get("channel")},
        )
        await session.commit()
        return {"status": "accepted", "case_id": str(result.case.id)}

    if kind in ("call.ended", "call.transcribed"):
        call_id = data.get("callId")
        if not call_id:
            return {"status": "ignored", "reason": "no callId"}
        phone_from, phone_to = data.get("from"), data.get("to")
        direction = data.get("direction")
        our_number = phone_from if direction == "outbound" else phone_to
        customer_number = phone_to if direction == "outbound" else phone_from
        business = await _business_for_number(session, our_number) if our_number else None
        if kind == "call.ended" and business is not None and customer_number:
            customer = await get_or_create_customer(session, business, phone=customer_number)
            case, _ = await find_case_for_customer(
                session,
                business,
                customer,
                now=now,
                reopen_window_days=services.settings.reopen_window_days,
            )
            await record_event(
                session,
                business_id=business.id,
                case_id=case.id if case else None,
                type="call_ended",
                actor=Sender.SYSTEM,
                data={
                    "call_id": call_id,
                    "status": data.get("status"),
                    "duration_seconds": data.get("durationSeconds"),
                    "transcript_available": data.get("transcriptAvailable"),
                },
            )
        want_transcript = kind == "call.transcribed" or bool(data.get("transcriptAvailable"))
        if want_transcript and business is not None:
            dup = await session.execute(
                select(Message.id).where(Message.provider_message_id == call_id).limit(1)
            )
            if dup.first() is None:
                await enqueue_job(
                    session,
                    business_id=business.id,
                    kind=JobKind.FETCH_CALL_TRANSCRIPT,
                    due_at=now,
                    payload={"call_id": call_id},
                )
        await session.commit()
        return {"status": "accepted"}

    return {"status": "ignored", "type": kind}


async def _business_for_number(session: SessionDep, number: str | None) -> Business | None:
    if not number:
        return None
    return (
        await session.execute(select(Business).where(Business.dial_phone_number == number))
    ).scalar_one_or_none()
