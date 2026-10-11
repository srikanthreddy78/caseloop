from __future__ import annotations

import json
import uuid
from typing import Any

import httpx
import respx
from app.agent.tools import CaseTools, TurnContext
from app.models import Business
from app.notifications import FakeNotifier
from app.orders.base import OrderRecord
from app.orders.providers import FakeOrderProvider
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_auth import OWNER, sign_in, token_from_email
from tests.integration.test_email_flow import run_jobs, simulate

CSV = "order_number,email,status,delivered_at,total\n55210,priya@customer-mail.co,delivered,2026-09-30,80.00\n"
MASK = "••••"


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


# ---- Configuring live sources --------------------------------------------------------------


async def _stored_sources(services: Any, business_id: str) -> list[dict[str, Any]]:
    """The raw configs as saved, secrets and all."""
    async with services.session_factory() as session:
        business = await session.get(Business, uuid.UUID(business_id))
        assert business is not None
        return list(business.settings.get("order_sources", []))


async def test_sources_can_only_be_changed_by_a_signed_in_owner(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    body = {"order_sources": [{"type": "shopify", "shop": "acme.myshopify.com", "token": "t"}]}
    assert (await client.put("/v1/orders/sources", json=body)).status_code == 401

    client.headers["Authorization"] = f"Bearer {business['api_key']}"
    resp = await client.put("/v1/orders/sources", json=body)
    assert resp.status_code == 403, resp.text
    assert (await client.get("/v1/orders/sources")).status_code == 200  # reading is fine
    del client.headers["Authorization"]

    await sign_in(client, notifier, OWNER)
    invite = await client.post("/v1/team", json={"email": "jo@acme-lamps.co", "name": "Jo"})
    assert invite.status_code == 201
    resp = await client.post(
        "/v1/auth/magic-link/exchange",
        json={"token": token_from_email(notifier, "jo@acme-lamps.co")},
    )
    assert resp.status_code == 200 and resp.json()["data"]["member"]["role"] == "member"
    resp = await client.put("/v1/orders/sources", json=body)
    assert resp.status_code == 403, resp.text

    await sign_in(client, notifier, OWNER)
    resp = await client.put("/v1/orders/sources", json=body)
    assert resp.status_code == 200, resp.text


async def test_sources_get_ids_and_secrets_follow_the_id_not_the_position(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier, services: Any
) -> None:
    await sign_in(client, notifier, OWNER)
    put = await client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [
                {"type": "shopify", "shop": "Acme.myshopify.com", "token": "shpat_secret"},
                {
                    "type": "custom_api",
                    "url_template": "https://shop.example/api/orders/{order_number}",
                    "headers": {"Authorization": "Bearer api-secret"},
                    "list_path": "data",
                    "mapping": {"status": "state", "status_map": {"done": "delivered"}},
                },
                {
                    "type": "amazon",
                    "client_id": "cid",
                    "client_secret": "csecret",
                    "refresh_token": "rtoken",
                    "marketplace_id": "ATVPDKIKX0DER",
                },
            ]
        },
    )
    assert put.status_code == 200, put.text
    shown = put.json()["data"]
    ids = [s["id"] for s in shown]
    assert len(set(ids)) == 3 and all(uuid.UUID(i) for i in ids)
    assert shown[0]["shop"] == "acme.myshopify.com" and shown[0]["token"] == MASK
    assert shown[0]["label"] == "Shopify"
    assert shown[1]["headers"] == MASK
    assert shown[1]["url_template"] == "https://shop.example/api/orders/{order_number}"
    assert shown[1]["list_path"] == "data"
    assert shown[1]["mapping"] == {"status": "state", "status_map": {"done": "delivered"}}
    # Everything outside the known plain keys is a secret, client_id included.
    assert shown[2]["client_id"] == shown[2]["client_secret"] == shown[2]["refresh_token"] == MASK
    assert shown[2]["marketplace_id"] == "ATVPDKIKX0DER"
    assert (await client.get("/v1/orders/sources")).json()["data"]["configured"] == shown

    # Re-save in a different order, masks and all: each secret stays with its own source.
    again = await client.put("/v1/orders/sources", json={"order_sources": shown[::-1]})
    assert again.status_code == 200, again.text
    assert [s["id"] for s in again.json()["data"]] == ids[::-1]
    stored = {s["id"]: s for s in await _stored_sources(services, business["id"])}
    assert stored[ids[0]]["token"] == "shpat_secret"
    assert stored[ids[1]]["headers"] == {"Authorization": "Bearer api-secret"}
    assert stored[ids[2]]["client_secret"] == "csecret"
    assert stored[ids[2]]["refresh_token"] == "rtoken"
    assert "label" not in stored[ids[0]]

    # Dropping one and adding a new one: the survivor keeps its secret, the new one gets an id.
    resp = await client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [
                shown[0],
                {"type": "shopify", "shop": "b.myshopify.com", "token": "t2"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    stored_list = await _stored_sources(services, business["id"])
    assert [s["token"] for s in stored_list] == ["shpat_secret", "t2"]
    assert stored_list[0]["id"] == ids[0] and stored_list[1]["id"] not in ids


async def test_kept_secret_is_dropped_when_the_destination_changes(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier, services: Any
) -> None:
    await sign_in(client, notifier, OWNER)
    put = await client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [
                {"type": "shopify", "shop": "acme.myshopify.com", "token": "shpat_secret"},
                {
                    "type": "custom_api",
                    "url_template": "https://shop.example/api/orders/{order_number}",
                    "headers": {"X-Key": "k"},
                },
            ]
        },
    )
    assert put.status_code == 200, put.text
    shopify, custom = put.json()["data"]

    moved = {**shopify, "shop": "other.myshopify.com"}
    resp = await client.put("/v1/orders/sources", json={"order_sources": [moved, custom]})
    assert resp.status_code == 422, resp.text
    assert "token must be entered again" in resp.json()["error"]["message"]

    moved_api = {**custom, "url_template": "https://elsewhere.example/o/{order_number}"}
    resp = await client.put("/v1/orders/sources", json={"order_sources": [shopify, moved_api]})
    assert resp.status_code == 422, resp.text
    assert "headers must be entered again" in resp.json()["error"]["message"]

    # A mask on a source we have never seen cannot stand in for a secret either.
    resp = await client.put(
        "/v1/orders/sources",
        json={"order_sources": [{"type": "shopify", "shop": "new.myshopify.com", "token": MASK}]},
    )
    assert resp.status_code == 422

    # Nothing above was saved; the original secrets still sit under the original destinations.
    stored = await _stored_sources(services, business["id"])
    assert stored[0]["shop"] == "acme.myshopify.com" and stored[0]["token"] == "shpat_secret"
    assert stored[1]["headers"] == {"X-Key": "k"}

    # With the secret typed again the move goes through.
    resp = await client.put(
        "/v1/orders/sources",
        json={"order_sources": [{**moved, "token": "shpat_new"}, custom]},
    )
    assert resp.status_code == 200, resp.text
    stored = await _stored_sources(services, business["id"])
    assert stored[0]["id"] == shopify["id"]
    assert stored[0]["shop"] == "other.myshopify.com" and stored[0]["token"] == "shpat_new"


async def test_source_destinations_are_validated(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier, monkeypatch: Any
) -> None:
    await sign_in(client, notifier, OWNER)

    async def put(cfg: dict[str, Any]) -> httpx.Response:
        return await client.put("/v1/orders/sources", json={"order_sources": [cfg]})

    assert (await put({"type": "magic"})).status_code == 422
    for shop in ("acme.com", "acme.myshopify.com.evil.io", "", "my shop.myshopify.com"):
        resp = await put({"type": "shopify", "shop": shop, "token": "t"})
        assert resp.status_code == 422, shop
        assert "myshopify.com" in resp.json()["error"]["message"]
    resp = await put({"type": "shopify", "shop": "https://acme.myshopify.com/", "token": "t"})
    assert resp.status_code == 200 and resp.json()["data"][0]["shop"] == "acme.myshopify.com"

    bad_urls = [
        "http://shop.example/orders/{order_number}",
        "https://10.0.0.8/orders/{order_number}",
        "https://169.254.169.254/latest/{order_number}",
        "https://[::1]/orders/{order_number}",
        "https://{order_number}.example/",
        "https://shop.example/orders",  # no placeholder
    ]
    for kind in ("custom_api", "tracking_page"):
        for url in bad_urls:
            resp = await put({"type": kind, "url_template": url})
            assert resp.status_code == 422, (kind, url)
    resp = await put(
        {
            "type": "custom_api",
            "url_template": "https://shop.example/orders/{order_number}",
            "email_url_template": "https://192.168.0.1/by-email?e={email}",
        }
    )
    assert resp.status_code == 422 and "email_url_template" in resp.json()["error"]["message"]

    # A hostname that resolves into private space is refused too.
    monkeypatch.setattr("app.netguard.resolve_host", lambda host: ["10.9.9.9"])
    resp = await put(
        {"type": "custom_api", "url_template": "https://inside.example/{order_number}"}
    )
    assert resp.status_code == 422 and "non-public" in resp.json()["error"]["message"]
    monkeypatch.setattr("app.netguard.resolve_host", lambda host: ["93.184.216.34"])

    # Development runs allow plain http to localhost, nothing else.
    resp = await put(
        {"type": "tracking_page", "url_template": "http://localhost:3000/t/{order_number}"}
    )
    assert resp.status_code == 200, resp.text
    resp = await put(
        {"type": "tracking_page", "url_template": "http://127.0.0.1:3000/t/{order_number}"}
    )
    assert resp.status_code == 422


# ---- Lookups ---------------------------------------------------------------------------------


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


async def test_live_lookup_replaces_the_stale_stored_copy(
    auth_client: AsyncClient, services: Any
) -> None:
    record = OrderRecord(
        source="fake", source_order_id="x1", order_number="77002", status="shipped"
    )
    provider = FakeOrderProvider([record])
    services.orders.extra_providers = [provider]
    try:
        first = await auth_client.post("/v1/orders/sources/test", json={"order_number": "77002"})
        assert [o["status"] for o in first.json()["data"]["found"]] == ["shipped"]
        assert len(provider.queries) == 1

        # The parcel arrives; the live system now says delivered while our copy says shipped.
        provider.records = [record.model_copy(update={"status": "delivered"})]
        second = await auth_client.post("/v1/orders/sources/test", json={"order_number": "77002"})
        found = second.json()["data"]["found"]
        assert [o["status"] for o in found] == ["delivered"], found  # fresh, and only once
        assert len(provider.queries) == 2  # the stored copy never short-circuits the live call

        stored = (await auth_client.get("/v1/orders", params={"q": "77002"})).json()["data"]
        assert [o["status"] for o in stored] == ["delivered"]  # the cache was overwritten

        # A live source that is gone leaves no ghost: its cached rows are not an answer.
        services.orders.extra_providers = []
        gone = await auth_client.post("/v1/orders/sources/test", json={"order_number": "77002"})
        assert gone.json()["data"]["found"] == []
    finally:
        services.orders.extra_providers = []


async def test_no_order_system_is_reported(auth_client: AsyncClient) -> None:
    test = await auth_client.post("/v1/orders/sources/test", json={"order_number": "1"})
    assert test.json()["data"]["no_order_system_connected"] is True


async def test_forwarded_order_email_is_imported(auth_client: AsyncClient) -> None:
    forward = {
        "to": "orders@" + BUSINESS_PAYLOAD["inbound_email"].split("@")[1],
        "from_email": "noreply@shop-platform.co",
        "subject": "Fwd: Your order #B7001 has shipped",
        "text": "Order #B7001 shipped 2026-10-01. Tracking 1Z999AA10123456784. Total $59.00",
    }
    # Nobody is trusted until the business says who sends its order emails.
    resp = await auth_client.post("/dev/simulate/email", json=forward)
    assert resp.status_code == 200 and "ignored" in resp.json()["error"]["message"]
    assert (await auth_client.get("/v1/orders", params={"q": "B7001"})).json()["data"] == []

    saved = await auth_client.patch(
        "/v1/businesses/me", json={"order_email_senders": [" Shop-Platform.co ", "x@y.co"]}
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["data"]["order_email_senders"] == ["shop-platform.co", "x@y.co"]
    resp = await auth_client.post("/dev/simulate/email", json=forward)
    assert resp.status_code == 200, resp.text
    found = await auth_client.get("/v1/orders", params={"q": "B7001"})
    assert found.json()["data"][0]["source_label"] == "Forwarded email"
    assert found.json()["data"][0]["status"] == "shipped"
    # The agent sees it is lower-trust than a connected system.
    lookup = await auth_client.post("/v1/orders/sources/test", json={"order_number": "B7001"})
    assert lookup.json()["data"]["found"][0]["notes"] == "imported from a forwarded email"

    bad = await auth_client.patch("/v1/businesses/me", json={"order_email_senders": ["not valid"]})
    assert bad.status_code == 422


async def test_stored_orders_count_as_a_connected_source_even_without_a_match(
    auth_client: AsyncClient,
) -> None:
    await auth_client.post("/v1/orders/import", json={"csv": CSV})
    test = await auth_client.post(
        "/v1/orders/sources/test", json={"order_number": "does-not-exist"}
    )
    data = test.json()["data"]
    assert data["found"] == []
    assert data["no_order_system_connected"] is False
    assert "CSV import" in data["sources_checked"]


# ---- What the agent is allowed to see ------------------------------------------------------


class ProbeRunner:
    """Calls lookup_order the way a model might: with someone else's email, hoping for their order."""

    name = "probe"

    def __init__(self, order_number: str, email: str) -> None:
        self.order_number = order_number
        self.email = email
        self.results: list[dict[str, Any]] = []
        self.snapshots: list[dict[str, Any]] = []

    async def run_turn(self, ctx: TurnContext, tools: CaseTools, system: str) -> dict[str, Any]:
        self.snapshots.append(json.loads(await tools.dispatch("get_case", {})))
        raw = await tools.dispatch(
            "lookup_order", {"order_number": self.order_number, "email": self.email}
        )
        self.results.append(json.loads(raw))
        return {}


async def test_agent_lookups_use_the_case_customer_and_hide_other_peoples_orders(
    auth_client: AsyncClient, services: Any
) -> None:
    csv = (
        "order_number,email,phone,status,total,customer\n"
        "55210,priya@customer-mail.co,,delivered,80.00,Priya\n"
        "66300,someone@else.co,+15550009999,shipped,120.00,Someone Else\n"
        "77400,,,processing,15.00,\n"
    )
    imp = await auth_client.post("/v1/orders/import", json={"csv": csv})
    assert imp.status_code == 201, imp.text

    probe = ProbeRunner(order_number="66300", email="someone@else.co")
    original = services.runner
    services.runner = probe
    try:
        case = await simulate(
            auth_client,
            "Where is my order? Order number 77400.",
            from_email="priya@customer-mail.co",
        )
        await run_jobs(auth_client)
    finally:
        services.runner = original

    # The tool ran with the customer's own email, not the one the model supplied: it matched the
    # asked-for number and Priya's own order, never someone@else.co's.
    [result] = probe.results
    assert "CSV import" in result["sources_checked"]
    by_number = {o["order_number"]: o for o in result["found"]}
    assert set(by_number) == {"66300", "55210"}
    assert by_number["66300"] == {
        "order_number": "66300",
        "status": "found_but_unverified",
        "verify": "ask the customer to confirm the email or phone used on the order",
    }
    assert by_number["55210"]["status"] == "delivered" and by_number["55210"]["total"] == 80.0
    dumped = json.dumps(result)
    assert "someone@else.co" not in dumped and "Someone Else" not in dumped
    assert "customer_email" not in dumped and "customer_name" not in dumped

    # The prefetch before the turn applied the same rule, and get_case showed its result.
    [snapshot] = probe.snapshots
    prefetched = {o["order_number"]: o for o in snapshot["orders"]}
    assert prefetched["77400"]["status"] == "processing"
    assert prefetched["55210"]["status"] == "delivered"
    assert "someone@else.co" not in json.dumps(snapshot["orders"])

    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    lookups = [t for t in timeline if t.get("type") == "order_lookup"]
    assert lookups, timeline
    for event in lookups:
        assert event["data"]["query"].get("email") == "priya@customer-mail.co"
        assert "someone@else.co" not in json.dumps(event)


@respx.mock
async def test_shopify_lookup_uses_the_stored_token(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    await sign_in(client, notifier, OWNER)
    put = await client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [{"type": "shopify", "shop": "acme.myshopify.com", "token": "shpat_1"}]
        },
    )
    assert put.status_code == 200, put.text
    again = await client.put("/v1/orders/sources", json={"order_sources": put.json()["data"]})
    assert again.status_code == 200, again.text

    route = respx.get("https://acme.myshopify.com/admin/api/2025-10/orders.json").mock(
        return_value=httpx.Response(200, json={"orders": []})
    )
    test = await client.post("/v1/orders/sources/test", json={"order_number": "1"})
    assert test.status_code == 200, test.text
    assert route.call_count == 1
    assert route.calls.last.request.headers["X-Shopify-Access-Token"] == "shpat_1"
