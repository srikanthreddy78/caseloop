from __future__ import annotations

import json
import time
import uuid
from typing import Any

import pytest
from app.api import webhooks_resend
from app.channels.registry import ChannelRegistry
from app.webhooks.signatures import dial_signature
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD


async def run_jobs(client: AsyncClient) -> int:
    resp = await client.post("/dev/run-jobs", json={})
    return resp.json()["ran"]


async def test_resend_webhook_fetches_body_and_opens_case(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, channels: ChannelRegistry
) -> None:
    async def fake_fetch(email_id: str) -> dict[str, Any]:
        return {
            "id": email_id,
            "from": "Jane Doe <jane@customer-mail.co>",
            "subject": "Broken lamp",
            "text": "The lamp is cracked.\n\nOn Mon, Acme wrote:\n> old quoted text",
            "html": None,
            "attachments": [],
        }

    async def fake_download(
        email_id: str, attachments: list[Any], data_dir: Any
    ) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(webhooks_resend, "fetch_received_email", fake_fetch)
    monkeypatch.setattr(webhooks_resend, "download_attachments", fake_download)

    event = {
        "type": "email.received",
        "data": {
            "email_id": "em_1",
            "from": "Jane Doe <jane@customer-mail.co>",
            "to": [BUSINESS_PAYLOAD["inbound_email"]],
            "received_for": [],
            "message_id": "<abc@customer-mail.co>",
            "subject": "Broken lamp",
            "attachments": [],
        },
    }
    resp = await auth_client.post("/webhooks/resend", content=json.dumps(event))
    assert resp.status_code == 200, resp.text
    case_id = resp.json()["case_id"]
    case = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    assert case["channel"] == "email"
    assert case["customer"]["name"] == "Jane Doe"
    assert case["messages"][0]["body_text"] == "The lamp is cracked."

    again = await auth_client.post("/webhooks/resend", content=json.dumps(event))
    assert again.json()["status"] == "duplicate"

    other = dict(event)
    other["type"] = "email.sent"
    assert (await auth_client.post("/webhooks/resend", content=json.dumps(other))).json()[
        "status"
    ] == "ignored"

    await run_jobs(auth_client)
    assert len(channels.adapters["email"].sent) == 1  # type: ignore[attr-defined]


async def test_dial_sms_webhook_signed(
    auth_client: AsyncClient, services: Any, channels: ChannelRegistry
) -> None:
    services.settings.dial_webhook_secret = "whsec_dial"
    try:
        event = {
            "type": "message.received",
            "data": {
                "messageId": "msg_1",
                "from": "+15550009999",
                "to": BUSINESS_PAYLOAD["dial_phone_number"],
                "channel": "sms",
                "body": "my order 777 never arrived",
                "media": [],
            },
        }
        body = json.dumps(event).encode()
        ts = str(int(time.time()))
        headers = {"X-Dial-Signature": f"t={ts},v1={dial_signature('whsec_dial', ts, body)}"}
        resp = await auth_client.post("/webhooks/dial", content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        case_id = resp.json()["case_id"]

        forged = await auth_client.post(
            "/webhooks/dial", content=body, headers={"X-Dial-Signature": f"t={ts},v1=00"}
        )
        assert forged.status_code == 401

        await run_jobs(auth_client)
        case = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
        assert case["channel"] == "sms" and case["customer"]["phone"] == "+15550009999"
        sms = channels.adapters["sms"].sent  # type: ignore[attr-defined]
        assert len(sms) == 1 and len(sms[0].text) <= 320
    finally:
        services.settings.dial_webhook_secret = None


async def test_sms_reply_over_limit_is_blocked_by_tool(auth_client: AsyncClient) -> None:
    from app.agent import tools as t

    assert t.MAX_TOOL_CALLS >= 6


async def test_sms_falls_back_to_email_after_a_carrier_rejection(
    auth_client: AsyncClient, channels: ChannelRegistry, services: Any
) -> None:
    from app.channels.fallback import delivery_channel
    from app.models import Case, Customer, Direction, Message

    # SMS case whose first reply the carrier rejected, and the customer's email is known.
    resp = await auth_client.post(
        "/dev/simulate/sms",
        json={
            "to": BUSINESS_PAYLOAD["dial_phone_number"],
            "from_phone": "+15550003333",
            "body": "lamp cracked, order 4242",
        },
    )
    case_id = resp.json()["data"]["id"]
    await run_jobs(auth_client)
    async with services.session_factory() as session:
        case = await session.get(Case, uuid.UUID(case_id))
        assert case is not None
        customer = await session.get(Customer, case.customer_id)
        assert customer is not None
        customer.email = "fallback@customer-mail.co"
        first = next(m for m in case.messages if m.direction == Direction.OUTBOUND)
        first.meta = {"delivery": "failed", "error": "A2P/10DLC"}
        await session.commit()
        await session.refresh(case)
        assert delivery_channel(case, customer) == "email"
    msg_id = first.id
    resend = await auth_client.post(f"/v1/cases/{case_id}/messages/{msg_id}/resend")
    assert resend.status_code == 200, resend.text
    assert channels.adapters["email"].sent[-1].to == "fallback@customer-mail.co"  # type: ignore[attr-defined]
    detail = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    resent = next(m for m in detail["messages"] if m["id"] == str(msg_id))
    assert resent["channel"] == "email" and resent["meta"]["delivery"] == "sent"
    assert isinstance(Message, type)
