"""Deliver case events to the business's own webhook URL, signed like Dial signs ours."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.service import enqueue_job
from app.models import Business, JobKind
from app.webhooks.signatures import dial_signature

log = logging.getLogger(__name__)


async def emit_case_event(
    session: AsyncSession,
    business: Business,
    *,
    event: str,
    case_id: uuid.UUID,
    data: dict[str, Any],
    now: datetime,
) -> None:
    if not business.outbound_webhook_url:
        return
    await enqueue_job(
        session,
        business_id=business.id,
        kind=JobKind.DELIVER_WEBHOOK,
        case_id=case_id,
        due_at=now,
        payload={"event": event, "case_id": str(case_id), "data": data, "at": now.isoformat()},
    )


async def deliver_business_webhook(
    business: Business, payload: dict[str, Any], *, now: datetime
) -> None:
    assert business.outbound_webhook_url
    body = json.dumps(
        {
            "type": payload.get("event"),
            "case_id": payload.get("case_id"),
            "data": payload.get("data"),
            "at": payload.get("at"),
        }
    ).encode()
    headers = {"Content-Type": "application/json"}
    if business.outbound_webhook_secret:
        ts = str(int(now.timestamp()))
        headers["X-Caseloop-Signature"] = (
            f"t={ts},v1={dial_signature(business.outbound_webhook_secret, ts, body)}"
        )
    async with httpx.AsyncClient(timeout=15) as http:
        resp = await http.post(business.outbound_webhook_url, content=body, headers=headers)
        resp.raise_for_status()
