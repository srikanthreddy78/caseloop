"""Dial webhook: inbound SMS, call lifecycle, transcripts."""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, HTTPException, Request, status

from app.api.deps import ServicesDep, SessionDep
from app.channels.dial_events import process_dial_event
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

    return await process_dial_event(session, services, event)
