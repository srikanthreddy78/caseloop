from __future__ import annotations

from typing import Any

from app.channels.dial_events import DialEventListener
from app.container import Services
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD


async def test_stream_event_opens_a_case_and_duplicates_are_ignored(
    auth_client: AsyncClient, services: Services, business: dict[str, Any]
) -> None:
    listener = DialEventListener(services)
    event = {
        "type": "message.received",
        "data": {
            "messageId": "msg_stream_1",
            "from": "+15550007777",
            "to": BUSINESS_PAYLOAD["dial_phone_number"],
            "channel": "sms",
            "body": "my lamp came broken, order 8801",
            "media": [],
        },
    }
    first = await listener.handle(event)
    assert first["status"] == "accepted"
    again = await listener.handle(event)
    assert again["status"] == "duplicate"
    assert (await listener.handle({"type": "webhook.ping", "data": {}}))["status"] == "ignored"
    cases = (await auth_client.get("/v1/cases", params={"state": "new"})).json()["data"]
    assert len(cases) == 1 and cases[0]["channel"] == "sms"


async def test_unknown_number_is_ignored(services: Services, business: dict[str, Any]) -> None:
    listener = DialEventListener(services)
    out = await listener.handle(
        {"type": "message.received", "data": {"to": "+10000000000", "from": "+1", "body": "hi"}}
    )
    assert out["status"] == "ignored"


async def test_carrier_rejection_flags_the_reply_as_undelivered(
    auth_client: AsyncClient, services: Services, business: dict[str, Any], channels: Any
) -> None:
    from tests.integration.test_email_flow import run_jobs

    listener = DialEventListener(services)
    await listener.handle(
        {
            "type": "message.received",
            "data": {
                "messageId": "msg_in_2",
                "from": "+15550007777",
                "to": BUSINESS_PAYLOAD["dial_phone_number"],
                "channel": "sms",
                "body": "lamp cracked",
                "media": [],
            },
        }
    )
    await run_jobs(auth_client)
    case_id = (await auth_client.get("/v1/cases")).json()["data"][0]["id"]
    detail = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    outbound = next(m for m in detail["messages"] if m["direction"] == "outbound")
    provider_id = channels.adapters["sms"].sent[-1]  # FakeAdapter record
    assert outbound["meta"]["delivery"] == "sent"
    # Dial reports the carrier rejection later, keyed by the provider message id.
    async with services.session_factory() as session:
        from app.models import Message
        from sqlalchemy import select

        row = (
            await session.execute(select(Message).where(Message.id == outbound["id"]))
        ).scalar_one()
        provider_message_id = row.provider_message_id
    out = await listener.handle(
        {
            "type": "message.status_changed",
            "data": {
                "messageId": provider_message_id,
                "changed": "delivery",
                "deliveryState": "undelivered",
                "deliveryError": "sender isn't registered for A2P/10DLC messaging",
            },
        }
    )
    assert out == {"status": "accepted", "delivery": "failed"}
    detail = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    outbound = next(m for m in detail["messages"] if m["direction"] == "outbound")
    assert outbound["meta"]["delivery"] == "failed" and "10DLC" in outbound["meta"]["error"]
    timeline = (await auth_client.get(f"/v1/cases/{case_id}/timeline")).json()["data"]
    assert any(t.get("type") == "delivery_failed" for t in timeline)
    del provider_id


async def test_finished_calls_are_queued_once(services: Services, business: dict[str, Any]) -> None:
    class Call:
        def __init__(self, cid: str, status: str, transcript: str | None) -> None:
            self.id, self.status, self.transcript = cid, status, transcript
            self.phone_number_id = BUSINESS_PAYLOAD["dial_number_id"]

    class FakeDial:
        async def list_calls(self) -> list[Call]:
            return [
                Call("call_1", "completed", "agent: hi\nuser: lamp broken"),
                Call("call_2", "in_progress", None),
                Call("call_3", "completed", None),
            ]

    services.channels.dial = FakeDial()
    try:
        listener = DialEventListener(services)
        assert await listener.sync_recent_calls() == 1
        assert await listener.sync_recent_calls() == 0  # already queued
    finally:
        services.channels.dial = None
