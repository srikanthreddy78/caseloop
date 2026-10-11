from __future__ import annotations

import uuid
from typing import Any

from app.api import deps
from app.auth import hash_api_key
from app.notifications import FakeNotifier
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
    # Drain the widget-key bucket directly rather than opening six hundred chats.
    assert deps.widget_key_limiter.capacity == 600
    for _ in range(int(deps.widget_key_limiter.capacity)):
        deps.widget_key_limiter.allow(f"widget:{key}")
    resp = await auth_client.post(
        "/v1/chat/sessions",
        json={"email": "pat@customer-mail.co", "message": "hi"},
        headers={"X-Widget-Key": key},
    )
    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["message"].startswith("rate limit exceeded")
    # The key bucket only applies when the key is sent; the IP bucket still has room.
    assert (await auth_client.get("/v1/chat/sessions/nope/messages")).status_code == 404
    gone = await auth_client.get("/v1/chat/sessions/nope/messages", headers={"X-Widget-Key": key})
    assert gone.status_code == 429


async def test_widget_ip_bucket_covers_every_chat_route(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    key = business["widget_public_key"]
    sess = await auth_client.post(
        "/v1/chat/sessions",
        json={"email": "pat@customer-mail.co", "message": "hi"},
        headers={"X-Widget-Key": key},
    )
    token = sess.json()["token"]
    for _ in range(int(deps.limiter.capacity)):
        deps.limiter.allow("widget-ip:127.0.0.1")
    limited = [
        await auth_client.get(f"/v1/chat/sessions/{token}/messages"),
        await auth_client.post(f"/v1/chat/sessions/{token}/messages", json={"text": "more"}),
        await auth_client.get(f"/v1/chat/sessions/{token}/stream"),
        await auth_client.get(f"/v1/chat/sessions/{token}/attachments/{uuid.uuid4()}/0"),
        await auth_client.post(
            f"/v1/chat/sessions/{token}/attachments",
            files=[("files", ("p.png", b"\x89PNG\r\n\x1a\n", "image/png"))],
        ),
        await auth_client.post(
            "/v1/chat/sessions",
            json={"email": "pat@customer-mail.co", "message": "hi"},
            headers={"X-Widget-Key": key},
        ),
    ]
    assert [r.status_code for r in limited] == [429] * 6
    assert all(r.headers["retry-after"] == "1" for r in limited)
    # The business API has its own bucket and is unaffected.
    assert (await auth_client.get("/v1/businesses/me")).status_code == 200


async def _drain_api_key(key: str) -> None:
    for _ in range(int(deps.limiter.capacity)):
        deps.limiter.allow(f"api:{hash_api_key(key)}")


async def test_orders_and_team_routers_share_the_api_bucket(
    auth_client: AsyncClient, business: dict[str, Any]
) -> None:
    assert (await auth_client.get("/v1/orders")).status_code == 200
    assert (await auth_client.get("/v1/team")).status_code == 200
    await _drain_api_key(business["api_key"])
    assert (await auth_client.get("/v1/orders")).status_code == 429
    assert (await auth_client.get("/v1/team")).status_code == 429
    assert (await auth_client.get("/v1/cases")).status_code == 429


async def test_magic_link_has_its_own_ip_and_email_limits(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    owner = business["escalation_email"]
    # Three links per address every ten minutes...
    for _ in range(3):
        assert (await client.post("/v1/auth/magic-link", json={"email": owner})).status_code == 202
    fourth = await client.post("/v1/auth/magic-link", json={"email": owner.upper()})
    assert fourth.status_code == 429, fourth.text
    assert fourth.headers["retry-after"] == "60"
    assert len(notifier.sent) == 3
    # ...and ten per IP a minute, whatever the address.
    sent = 0
    for i in range(20):
        resp = await client.post("/v1/auth/magic-link", json={"email": f"p{i}@example.net"})
        if resp.status_code == 429:
            break
        sent += 1
    assert resp.status_code == 429
    # 10 per IP; the four requests above (the refused one included) already counted.
    assert 6 <= sent <= 7, sent
    # The exchange route is covered by the general per-IP bucket, not the sign-in one.
    assert (
        await client.post("/v1/auth/magic-link/exchange", json={"token": "nope"})
    ).status_code == 401
