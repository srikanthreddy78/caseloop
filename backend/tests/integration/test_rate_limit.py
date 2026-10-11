from __future__ import annotations

from typing import Any

from app.api import deps
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD


async def test_api_key_bucket_returns_429_with_error_envelope(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    statuses: list[int] = []
    for _ in range(int(deps.limiter.capacity) + 10):
        resp = await auth_client.get("/v1/businesses/me")
        statuses.append(resp.status_code)
        if resp.status_code == 429:
            break
    assert statuses[-1] == 429, statuses
    # The bucket refills while we loop, so allow a token or two of slack above the burst size.
    assert statuses.count(200) >= deps.limiter.capacity
    assert resp.json() == {
        "error": {"code": "error", "message": "rate limit exceeded, try again shortly"}
    }
    assert resp.headers["retry-after"] == "1"

    # The widget key has its own bucket, so chat still works for this business...
    chat = await auth_client.post(
        "/v1/chat/sessions",
        json={"email": "pat@customer-mail.co", "message": "still here"},
        headers={"X-Widget-Key": business["widget_public_key"]},
    )
    assert chat.status_code == 201, chat.text

    # ...and another business is unaffected.
    other = await auth_client.post(
        "/dev/businesses",
        json={
            **BUSINESS_PAYLOAD,
            "slug": "other",
            "inbound_email": "support@other.caseloop.dev",
            "dial_phone_number": None,
        },
    )
    assert other.status_code == 201
    ok = await auth_client.get(
        "/v1/businesses/me",
        headers={"Authorization": f"Bearer {other.json()['data']['api_key']}"},
    )
    assert ok.status_code == 200


async def test_widget_key_bucket_limits_session_creation(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    key = business["widget_public_key"]
    # Drain the widget bucket directly rather than opening sixty chats.
    for _ in range(int(deps.limiter.capacity)):
        deps.limiter.allow(f"widget:{key}")
    resp = await auth_client.post(
        "/v1/chat/sessions",
        json={"email": "pat@customer-mail.co", "message": "hi"},
        headers={"X-Widget-Key": key},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["message"].startswith("rate limit exceeded")
    # Polling an existing session is not limited (it is per-token and cheap).
    assert (await auth_client.get("/v1/chat/sessions/nope/messages")).status_code == 404
