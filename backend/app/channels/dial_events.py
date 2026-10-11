"""Dial events: one handler for webhook deliveries and the account event stream."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.service import (
    enqueue_job,
    find_case_for_customer,
    get_or_create_customer,
    ingest_inbound,
    record_event,
)
from app.models import Business, Case, Channel, JobKind, Message, Sender

log = logging.getLogger(__name__)

HANDLED_EVENTS = ("message.received", "message.status_changed", "call.ended", "call.transcribed")


async def process_dial_event(
    session: AsyncSession, services: Any, event: dict[str, Any]
) -> dict[str, str]:
    """Apply one Dial event (webhook body or stream item). Commits on success."""
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

    if kind == "message.status_changed":
        return await _apply_delivery_status(session, data)

    return {"status": "ignored", "type": kind}


async def _apply_delivery_status(session: AsyncSession, data: dict[str, Any]) -> dict[str, str]:
    """Carrier outcomes arrive after the send: flag undelivered texts on the stored message."""
    if data.get("changed", "delivery") != "delivery":
        return {"status": "ignored", "reason": "not a delivery update"}
    message_id = data.get("messageId")
    state = str(data.get("deliveryState") or "")
    if not message_id or state not in ("delivered", "undelivered", "failed"):
        return {"status": "ignored", "reason": "no delivery outcome"}
    message = (
        await session.execute(select(Message).where(Message.provider_message_id == message_id))
    ).scalar_one_or_none()
    if message is None:
        return {"status": "ignored", "reason": "unknown message"}
    case = await session.get(Case, message.case_id)
    if case is None:
        return {"status": "ignored", "reason": "unknown case"}
    meta = dict(message.meta or {})
    if state == "delivered":
        meta.update({"delivery": "sent", "delivery_state": state})
        meta.pop("error", None)
    else:
        meta.update(
            {
                "delivery": "failed",
                "delivery_state": state,
                "error": data.get("deliveryError") or state,
            }
        )
    message.meta = meta
    await record_event(
        session,
        business_id=case.business_id,
        case_id=message.case_id,
        type="delivery_failed" if state != "delivered" else "delivery_confirmed",
        actor=Sender.SYSTEM,
        data={
            "message_id": str(message.id),
            "channel": message.channel,
            "error": meta.get("error"),
        },
    )
    await session.commit()
    return {"status": "accepted", "delivery": meta["delivery"]}


async def _business_for_number(session: AsyncSession, number: str | None) -> Business | None:
    if not number:
        return None
    return (
        await session.execute(select(Business).where(Business.dial_phone_number == number))
    ).scalar_one_or_none()


def _as_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        return event
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(event, attr, None)
        if callable(fn):
            out = fn()
            if isinstance(out, dict):
                return out
    return {"type": getattr(event, "type", None), "data": getattr(event, "data", None) or {}}


class DialEventListener:
    """Consumes the account event stream so local and single-host deployments need no webhook."""

    def __init__(self, services: Any) -> None:
        self.services = services
        self._stop = asyncio.Event()

    async def handle(self, raw: Any) -> dict[str, str]:
        event = _as_dict(raw)
        if str(event.get("type", "")) not in HANDLED_EVENTS:
            return {"status": "ignored"}
        async with self.services.session_factory() as session:
            try:
                return await process_dial_event(session, self.services, event)
            except Exception:
                await session.rollback()
                log.exception("dial event %s failed", event.get("type"))
                return {"status": "error"}

    async def run_forever(self) -> None:
        client = getattr(self.services.channels, "dial", None)
        if client is None:
            return
        delay = 1.0
        log.info("dial event stream listener started")
        while not self._stop.is_set():
            try:
                async with client.new_events_connection() as conn:
                    delay = 1.0
                    async for raw in conn:
                        if self._stop.is_set():
                            break
                        await self.handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(
                    "dial event stream dropped (%s); reconnecting in %.0fs", str(exc)[:200], delay
                )
            if self._stop.is_set():
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 60.0)

    def stop(self) -> None:
        self._stop.set()
