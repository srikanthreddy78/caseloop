from __future__ import annotations

from typing import Any

from app.orders.base import OrderRecord
from app.orders.providers import FakeOrderProvider
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_email_flow import run_jobs, simulate

CSV = "order_number,email,status,delivered_at,total\n55210,priya@customer-mail.co,delivered,2026-09-30,80.00\n"


async def test_import_push_search_and_sources(auth_client: AsyncClient) -> None:
    imp = await auth_client.post("/v1/orders/import", json={"csv": CSV})
    assert imp.status_code == 201, imp.text
    assert (
        imp.json()["data"]["imported"] == 1 and imp.json()["data"]["source_label"] == "CSV import"
    )

    push = await auth_client.post(
        "/v1/orders",
        json={
            "orders": [
                {"order_number": "9001", "email": "tom@customer-mail.co", "status": "shipped"}
            ],
            "source": "api",
        },
    )
    assert push.status_code == 201 and push.json()["data"]["imported"] == 1

    found = await auth_client.get("/v1/orders", params={"q": "#55210"})
    assert [o["order_number"] for o in found.json()["data"]] == ["55210"]
    assert found.json()["data"][0]["source_label"] == "CSV import"

    sources = await auth_client.get("/v1/orders/sources")
    data = sources.json()["data"]
    assert data["stored_counts"]["csv"]["count"] == 1 and data["stored_counts"]["api"]["count"] == 1
    assert data["forward_address"] == "orders@acme.caseloop.dev"

    put = await auth_client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [
                {"type": "shopify", "shop": "acme.myshopify.com", "token": "shpat_secret"}
            ]
        },
    )
    assert put.status_code == 200
    assert put.json()["data"][0]["token"] == "••••" and put.json()["data"][0]["label"] == "Shopify"
    # Re-saving with the mask keeps the real token.
    again = await auth_client.put("/v1/orders/sources", json={"order_sources": put.json()["data"]})
    assert again.status_code == 200
    me = await auth_client.get("/v1/businesses/me")
    assert me.status_code == 200

    bad = await auth_client.put("/v1/orders/sources", json={"order_sources": [{"type": "magic"}]})
    assert bad.status_code == 422


async def test_agent_sees_stored_order_before_acting(auth_client: AsyncClient) -> None:
    await auth_client.post("/v1/orders/import", json={"csv": CSV})
    case = await simulate(
        auth_client, "Order 55210 never arrived!", from_email="priya@customer-mail.co"
    )
    await run_jobs(auth_client)
    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    lookups = [t for t in timeline if t.get("type") == "order_lookup"]
    assert lookups and lookups[0]["data"]["found"] == 1
    assert "CSV import" in lookups[0]["data"]["sources"]


async def test_live_provider_results_are_labeled_and_stored(
    auth_client: AsyncClient, services: Any
) -> None:
    services.orders.extra_providers = [
        FakeOrderProvider(
            [
                OrderRecord(
                    source="fake",
                    source_order_id="x1",
                    order_number="77001",
                    status="delivered",
                    delivered_at="2026-09-29",
                    customer_email="marcus@customer-mail.co",
                )
            ]
        )
    ]
    try:
        test = await auth_client.post("/v1/orders/sources/test", json={"order_number": "77001"})
        assert test.status_code == 200, test.text
        data = test.json()["data"]
        assert (
            data["found"][0]["source"] == "Test data" and data["found"][0]["status"] == "delivered"
        )
        assert "Test data" in data["sources_checked"] and data["no_order_system_connected"] is False
        stored = await auth_client.get("/v1/orders", params={"q": "77001"})
        assert stored.json()["data"][0]["source"] == "fake"
    finally:
        services.orders.extra_providers = []


async def test_no_order_system_is_reported(auth_client: AsyncClient) -> None:
    test = await auth_client.post("/v1/orders/sources/test", json={"order_number": "1"})
    assert test.json()["data"]["no_order_system_connected"] is True


async def test_forwarded_order_email_is_imported(auth_client: AsyncClient) -> None:
    resp = await auth_client.post(
        "/dev/simulate/email",
        json={
            "to": "orders@" + BUSINESS_PAYLOAD["inbound_email"].split("@")[1],
            "from_email": "noreply@shop-platform.co",
            "subject": "Fwd: Your order #B7001 has shipped",
            "text": "Order #B7001 shipped 2026-10-01. Tracking 1Z999AA10123456784. Total $59.00",
        },
    )
    assert resp.status_code == 200, resp.text
    found = await auth_client.get("/v1/orders", params={"q": "B7001"})
    assert found.json()["data"][0]["source_label"] == "Forwarded email"
    assert found.json()["data"][0]["status"] == "shipped"
