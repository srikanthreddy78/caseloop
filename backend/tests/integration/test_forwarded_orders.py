"""Forwarded order emails through the Resend webhook: only trusted senders are imported."""

from __future__ import annotations

import json
from typing import Any

import pytest
from app.api import webhooks_resend
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD

FORWARD_ADDRESS = "orders@" + BUSINESS_PAYLOAD["inbound_email"].split("@")[1]


def _received(sender: str, dkim: str | None) -> dict[str, Any]:
    return {
        "id": "em_fwd",
        "from": sender,
        "subject": "Fwd: Your order #C9001 has shipped",
        "text": "Order #C9001 shipped 2026-10-02. Tracking 1Z999AA10123456785. Total $19.00",
        "html": None,
        "attachments": [],
        "authentication": {"spf": "pass", "dkim": dkim, "dmarc": "pass"} if dkim else None,
    }


async def _post(client: AsyncClient, monkeypatch: pytest.MonkeyPatch, received: dict) -> dict:
    async def fake_fetch(email_id: str) -> dict[str, Any]:
        return received

    monkeypatch.setattr(webhooks_resend, "fetch_received_email", fake_fetch)
    event = {
        "type": "email.received",
        "data": {"email_id": "em_fwd", "from": received["from"], "to": [FORWARD_ADDRESS]},
    }
    resp = await client.post("/webhooks/resend", content=json.dumps(event))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _orders(client: AsyncClient) -> list[dict[str, Any]]:
    return (await client.get("/v1/orders", params={"q": "C9001"})).json()["data"]


async def test_unknown_sender_is_ignored(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await auth_client.patch("/v1/businesses/me", json={"order_email_senders": ["shopify.com"]})
    out = await _post(auth_client, monkeypatch, _received("Mallory <mallory@evil.example>", "pass"))
    assert out == {"status": "ignored", "reason": "sender not in order_email_senders"}
    assert await _orders(auth_client) == []


async def test_listed_address_or_domain_is_imported(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await auth_client.patch(
        "/v1/businesses/me", json={"order_email_senders": ["orders@shopify.com", "amazon.com"]}
    )
    out = await _post(auth_client, monkeypatch, _received("Shopify <ORDERS@shopify.com>", None))
    assert out["status"] == "orders_imported" and out["imported"] == "1"
    out = await _post(auth_client, monkeypatch, _received("auto-confirm@mail.amazon.com", None))
    assert out["status"] == "orders_imported"
    # A different mailbox on a listed-address-only domain is not trusted by itself.
    out = await _post(auth_client, monkeypatch, _received("noreply@shopify.com", None))
    assert out["status"] == "ignored"
    orders = await _orders(auth_client)
    assert orders and orders[0]["source_label"] == "Forwarded email"


async def test_dkim_pass_for_listed_domain_is_trusted(
    auth_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    await auth_client.patch(
        "/v1/businesses/me", json={"order_email_senders": ["orders@shopify.com", "shopify.com"]}
    )
    out = await _post(auth_client, monkeypatch, _received("noreply@shopify.com", "fail"))
    # The domain itself is listed, so the address matches without DKIM...
    assert out["status"] == "orders_imported"
    await auth_client.patch("/v1/businesses/me", json={"order_email_senders": ["shopify.com"]})
    # ...and with only the domain listed a signed mail from any mailbox there is accepted.
    out = await _post(auth_client, monkeypatch, _received("x@shopify.com", "pass"))
    assert out["status"] == "orders_imported"
