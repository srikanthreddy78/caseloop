"""Resend Receiving webhook: email.received -> fetch body -> ingest."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from sqlalchemy import select

from app.api.deps import ServicesDep, SessionDep
from app.cases.service import get_or_create_customer, ingest_inbound
from app.channels.email import (
    download_attachments,
    fetch_received_email,
    html_to_text,
    parse_sender,
    strip_quoted_reply,
)
from app.models import Business, Channel, Message
from app.orders.email_forward import business_for_forward_address, import_forwarded_email
from app.webhooks.signatures import InvalidSignature, verify_resend

log = logging.getLogger(__name__)
router = APIRouter(prefix="/webhooks", tags=["webhooks"])


@router.post("/resend")
async def resend_webhook(
    request: Request, session: SessionDep, services: ServicesDep
) -> dict[str, str]:
    raw = await request.body()
    secret = services.settings.resend_webhook_secret
    if secret:
        try:
            verify_resend(secret, raw, request.headers)
        except InvalidSignature as exc:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"bad signature: {exc}") from exc
    elif not services.settings.is_dev:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "RESEND_WEBHOOK_SECRET is not configured"
        )

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid JSON") from exc
    if event.get("type") != "email.received":
        return {"status": "ignored", "type": str(event.get("type"))}
    data = event.get("data") or {}
    email_id = data.get("email_id")
    if not email_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "missing data.email_id")

    recipients = [str(a).lower() for a in (data.get("to") or []) + (data.get("received_for") or [])]
    recipients = [parse_sender(a)[1] for a in recipients]
    business = (
        await session.execute(select(Business).where(Business.inbound_email.in_(recipients)))
    ).scalar_one_or_none()
    if business is None:
        forward_biz = await business_for_forward_address(session, recipients)
        if forward_biz is not None:
            received = await fetch_received_email(email_id)
            text = (received.get("text") or "").strip() or html_to_text(received.get("html") or "")
            n = await import_forwarded_email(
                session,
                forward_biz,
                text,
                extractor=services.orders.extractor,
                now=services.clock.now(),
            )
            await session.commit()
            return {"status": "orders_imported", "imported": str(n)}
        log.info("resend webhook for unknown recipients %s", recipients)
        return {"status": "ignored", "reason": "no business for recipient"}

    rfc_id = data.get("message_id")
    if rfc_id:
        dup = await session.execute(
            select(Message.id).where(Message.provider_message_id == rfc_id).limit(1)
        )
        if dup.first() is not None:
            return {"status": "duplicate"}

    received = await fetch_received_email(email_id)
    text = (received.get("text") or "").strip() or html_to_text(received.get("html") or "")
    text = strip_quoted_reply(text) or "(empty message)"
    attachments = await download_attachments(
        email_id,
        list(received.get("attachments") or data.get("attachments") or []),
        services.settings.data_dir,
    )
    name, addr = parse_sender(str(received.get("from") or data.get("from") or ""))
    if not addr:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "sender address missing")
    customer = await get_or_create_customer(session, business, email=addr, name=name)
    result = await ingest_inbound(
        session,
        business,
        customer,
        channel=Channel.EMAIL,
        text=text,
        html=received.get("html"),
        subject=received.get("subject") or data.get("subject"),
        provider_message_id=rfc_id,
        provider_thread_ids={"resend_email_id": email_id},
        attachments=attachments,
        now=services.clock.now(),
        reopen_window_days=services.settings.reopen_window_days,
        meta={"source": "resend", "authentication": received.get("authentication")},
    )
    await session.commit()
    return {"status": "accepted", "case_id": str(result.case.id)}
