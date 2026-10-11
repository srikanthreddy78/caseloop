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
