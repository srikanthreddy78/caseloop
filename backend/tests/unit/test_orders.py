from __future__ import annotations

import logging
from typing import Any

import httpx
import pytest
import respx
from app.netguard import UnsafeUrlError
from app.orders.base import OrderQuery, OrderRecord, source_label
from app.orders.extract import ScriptedOrderExtractor
from app.orders.imports import parse_csv, rows_to_records
from app.orders.providers import (
    CustomApiProvider,
    FakeOrderProvider,
    ShopifyProvider,
    TrackingPageProvider,
    normalize_status,
)
from app.orders.service import LookupResult, mask_source


def test_source_labels_are_human() -> None:
    assert source_label("shopify") == "Shopify"
    assert source_label("custom_api") == "Your website API"
    assert source_label("email_forward") == "Forwarded email"
    assert source_label("something_new") == "Something New"


def test_csv_import_maps_common_columns_and_groups_items() -> None:
    csv_text = (
        "Order ID,Email,Status,Order Date,Tracking,Item,Qty,Total\n"
        "#1001,a@x.co,Shipped,2026-09-01,1Z999AA10123456784,Lamp,1,89.00\n"
        "#1001,a@x.co,Shipped,2026-09-01,1Z999AA10123456784,Bulb,2,89.00\n"
        "1002,b@x.co,delivered,2026-09-03,,Desk lamp,1,120.50\n"
    )
    records = rows_to_records(parse_csv(csv_text), source="csv")
    assert [r.order_number for r in records] == ["1001", "1002"]
    first = records[0]
    assert (
        first.status == "shipped"
        and first.tracking
        and first.tracking.number == "1Z999AA10123456784"
    )
    assert [(i.name, i.quantity) for i in first.items] == [("Lamp", 1), ("Bulb", 2)]
    assert records[1].status == "delivered" and records[1].total == 120.5


def test_csv_import_without_order_column_fails_clearly() -> None:
    with pytest.raises(ValueError, match="order number column"):
        rows_to_records([{"foo": "1"}], source="csv")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Fulfilled", "shipped"),  # fulfilled means it left the warehouse, not that it arrived
        ("Complete", "delivered"),
        ("completed", "delivered"),
        ("in transit", "shipped"),
        ("weird", "unknown"),
        (None, "unknown"),
    ],
)
def test_status_normalization(raw: str | None, expected: str) -> None:
    assert normalize_status(raw) == expected


def test_status_map_overrides_the_defaults() -> None:
    overrides = {"Done": "delivered", "complete": "processing", "odd": "not-a-status"}
    assert normalize_status("done", overrides) == "delivered"
    assert normalize_status("Complete", overrides) == "processing"  # their word, their meaning
    assert normalize_status("completed", overrides) == "delivered"  # untouched default
    assert normalize_status("odd", overrides) == "unknown"


async def test_scripted_extractor_reads_a_forwarded_email() -> None:
    text = "Thanks for your order #A5501! Total: $149.99. Shipped 2026-09-10, tracking 1Z999AA10123456784. jane@x.co"
    recs = await ScriptedOrderExtractor().extract(text, source="email_forward")
    assert len(recs) == 1
    r = recs[0]
    assert r.order_number == "A5501" and r.status == "shipped" and r.total == 149.99
    assert (
        r.tracking and r.tracking.number == "1Z999AA10123456784" and r.customer_email == "jane@x.co"
    )


@respx.mock
async def test_custom_api_provider_maps_business_json() -> None:
    respx.get("https://shop.example/api/orders/7781").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "ref": "7781",
                    "state": "Complete",
                    "delivered_on": "2026-09-12",
                    "ship": {"trk": "9400111899223"},
                    "amount": "42.00",
                    "lines": [{"title": "Wall sconce", "qty": 2}],
                }
            },
        )
    )
    provider = CustomApiProvider(
        {
            "url_template": "https://shop.example/api/orders/{order_number}",
            "headers": {"X-Key": "k"},
            "list_path": "data",
            "mapping": {
                "order_number": "ref",
                "status": "state",
                "delivered_at": "delivered_on",
                "tracking_number": "ship.trk",
                "total": "amount",
                "items": "lines",
                "item_name": "title",
                "item_qty": "qty",
            },
        }
    )
    [rec] = await provider.lookup(OrderQuery(order_number="#7781"))
    assert rec.source == "custom_api" and rec.order_number == "7781"
    assert rec.status == "delivered" and rec.delivered_at == "2026-09-12" and rec.total == 42.0
    assert rec.tracking and rec.tracking.number == "9400111899223"
    assert [(i.name, i.quantity) for i in rec.items] == [("Wall sconce", 2)]
    assert respx.calls.last.request.headers["X-Key"] == "k"


@respx.mock
async def test_custom_api_404_means_not_found() -> None:
    respx.get("https://shop.example/api/orders/1").mock(return_value=httpx.Response(404))
    provider = CustomApiProvider({"url_template": "https://shop.example/api/orders/{order_number}"})
    assert await provider.lookup(OrderQuery(order_number="1")) == []


@respx.mock
async def test_tracking_page_provider_extracts_from_html() -> None:
    respx.get("https://shop.example/track?order=5550").mock(
        return_value=httpx.Response(
            200,
            headers={"content-type": "text/html"},
            text="<html><body><h1>Order #5550</h1><p>Status: Delivered on 2026-09-20</p></body></html>",
        )
    )
    provider = TrackingPageProvider(
        {"url_template": "https://shop.example/track?order={order_number}"},
        ScriptedOrderExtractor(),
    )
    [rec] = await provider.lookup(OrderQuery(order_number="5550"))
    assert (
        rec.source == "tracking_page"
        and rec.status == "delivered"
        and rec.delivered_at == "2026-09-20"
    )


@respx.mock
async def test_shopify_provider_maps_fulfillment() -> None:
    respx.get("https://acme.myshopify.com/admin/api/2025-10/orders.json").mock(
        return_value=httpx.Response(
            200,
            json={
                "orders": [
                    {
                        "id": 9,
                        "name": "#1042",
                        "email": "c@x.co",
                        "created_at": "2026-09-01T10:00:00Z",
                        "total_price": "75.00",
                        "currency": "USD",
                        "financial_status": "paid",
                        "fulfillment_status": "fulfilled",
                        "line_items": [{"title": "Lamp", "quantity": 1, "price": "75.00"}],
                        "fulfillments": [
                            {
                                "tracking_company": "UPS",
                                "tracking_number": "1Z1",
                                "shipment_status": "delivered",
                                "created_at": "2026-09-02T00:00:00Z",
                                "updated_at": "2026-09-05T00:00:00Z",
                            }
                        ],
                    }
                ]
            },
        )
    )
    provider = ShopifyProvider({"shop": "acme.myshopify.com", "token": "shpat_x"})
    [rec] = await provider.lookup(OrderQuery(order_number="1042"))
    assert rec.source == "shopify" and rec.order_number == "1042" and rec.status == "delivered"
    assert (
        rec.delivered_at == "2026-09-05T00:00:00Z"
        and rec.tracking
        and rec.tracking.carrier == "UPS"
    )
    assert respx.calls.last.request.headers["X-Shopify-Access-Token"] == "shpat_x"
    assert "name=%231042" in str(respx.calls.last.request.url)


async def test_fake_provider_matches_by_number_or_email() -> None:
    rec = OrderRecord(
        source="fake",
        source_order_id="1",
        order_number="A1",
        status="shipped",
        customer_email="z@x.co",
    )
    p = FakeOrderProvider([rec])
    assert await p.lookup(OrderQuery(order_number="#a1")) == [rec]
    assert await p.lookup(OrderQuery(email="Z@x.co")) == [rec]
    assert await p.lookup(OrderQuery(order_number="nope")) == []


# ---- Shopify status ------------------------------------------------------------------------


def _shopify_order(**overrides: Any) -> dict[str, Any]:
    order: dict[str, Any] = {
        "id": 9,
        "name": "#1042",
        "email": "c@x.co",
        "created_at": "2026-09-01T10:00:00Z",
        "total_price": "75.00",
        "currency": "USD",
        "financial_status": "paid",
        "fulfillment_status": "fulfilled",
        "line_items": [{"title": "Lamp", "quantity": 1, "price": "75.00"}],
        "fulfillments": [
            {
                "tracking_company": "UPS",
                "tracking_number": "1Z1",
                "shipment_status": "in_transit",
                "created_at": "2026-09-02T00:00:00Z",
                "updated_at": "2026-09-03T00:00:00Z",
            }
        ],
    }
    order.update(overrides)
    return order


@respx.mock
async def test_shopify_fulfilled_means_shipped_until_the_carrier_says_delivered() -> None:
    respx.get("https://acme.myshopify.com/admin/api/2025-10/orders.json").mock(
        return_value=httpx.Response(200, json={"orders": [_shopify_order()]})
    )
    provider = ShopifyProvider({"shop": "acme.myshopify.com", "token": "shpat_x"})
    [rec] = await provider.lookup(OrderQuery(order_number="1042"))
    assert rec.status == "shipped" and rec.delivered_at is None
    assert rec.shipped_at == "2026-09-02T00:00:00Z"


@respx.mock
async def test_shopify_fulfilled_without_any_fulfillment_record_is_still_shipped() -> None:
    respx.get("https://acme.myshopify.com/admin/api/2025-10/orders.json").mock(
        return_value=httpx.Response(200, json={"orders": [_shopify_order(fulfillments=[])]})
    )
    provider = ShopifyProvider({"shop": "acme.myshopify.com", "token": "shpat_x"})
    [rec] = await provider.lookup(OrderQuery(order_number="1042"))
    assert rec.status == "shipped" and rec.delivered_at is None


@respx.mock
async def test_shopify_skips_an_order_it_cannot_map(caplog: pytest.LogCaptureFixture) -> None:
    bad = _shopify_order(id=10, name="#1043", line_items=[{"title": "Lamp", "quantity": "two"}])
    respx.get("https://acme.myshopify.com/admin/api/2025-10/orders.json").mock(
        return_value=httpx.Response(200, json={"orders": [bad, _shopify_order()]})
    )
    provider = ShopifyProvider({"shop": "acme.myshopify.com", "token": "shpat_x"})
    with caplog.at_level(logging.WARNING, logger="app.orders.providers"):
        recs = await provider.lookup(OrderQuery(email="c@x.co"))
    assert [r.order_number for r in recs] == ["1042"]
    assert "skipping an order" in caplog.text


# ---- Custom API mapping ----------------------------------------------------------------------


@respx.mock
async def test_custom_api_status_map_and_source_id_prefix() -> None:
    respx.get("https://shop.example/api/orders/7781").mock(
        return_value=httpx.Response(200, json={"id": 7781, "status": "on_the_way"})
    )
    provider = CustomApiProvider(
        {
            "id": "src-a",
            "url_template": "https://shop.example/api/orders/{order_number}",
            "mapping": {"status_map": {"on_the_way": "shipped"}},
        }
    )
    [rec] = await provider.lookup(OrderQuery(order_number="7781"))
    assert rec.status == "shipped"
    assert rec.source_order_id == "src-a:7781"
    assert "status_map" not in provider.mapping


@respx.mock
async def test_two_custom_sources_never_collide_on_the_same_order_number() -> None:
    respx.get("https://a.example/o/1").mock(return_value=httpx.Response(200, json={"id": 1}))
    respx.get("https://b.example/o/1").mock(return_value=httpx.Response(200, json={"id": 1}))
    a = CustomApiProvider({"id": "aaa", "url_template": "https://a.example/o/{order_number}"})
    b = CustomApiProvider({"id": "bbb", "url_template": "https://b.example/o/{order_number}"})
    [ra] = await a.lookup(OrderQuery(order_number="1"))
    [rb] = await b.lookup(OrderQuery(order_number="1"))
    assert ra.order_number == rb.order_number == "1"
    assert ra.source_order_id != rb.source_order_id


@respx.mock
async def test_custom_api_skips_a_bad_row_instead_of_failing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    respx.get("https://shop.example/api/orders/by-email").mock(
        return_value=httpx.Response(
            200,
            json=[
                {"id": "A1", "total": "not a number"},
                {"id": "A2", "total": "12.50", "items": [{"name": "Lamp", "quantity": "many"}]},
                {"id": "A3", "total": "9.99"},
                "not even an object",
            ],
        )
    )
    provider = CustomApiProvider(
        {
            "id": "src",
            "url_template": "https://shop.example/api/orders/{order_number}",
            "email_url_template": "https://shop.example/api/orders/by-email",
        }
    )
    with caplog.at_level(logging.WARNING, logger="app.orders.providers"):
        recs = await provider.lookup(OrderQuery(email="z@x.co"))
    assert [r.order_number for r in recs] == ["A3"]
    assert caplog.text.count("skipping a row") == 2


@respx.mock
async def test_tracking_page_records_are_keyed_by_source_id() -> None:
    respx.get("https://shop.example/track?order=5550").mock(
        return_value=httpx.Response(200, text="Order #5550 Status: Delivered on 2026-09-20")
    )
    provider = TrackingPageProvider(
        {"id": "tp-1", "url_template": "https://shop.example/track?order={order_number}"},
        ScriptedOrderExtractor(),
    )
    [rec] = await provider.lookup(OrderQuery(order_number="5550"))
    assert rec.source_order_id == "tp-1:5550"


# ---- Outbound URL guard ----------------------------------------------------------------------


@respx.mock
async def test_providers_do_not_follow_redirects() -> None:
    respx.get("https://shop.example/api/orders/1").mock(
        return_value=httpx.Response(302, headers={"location": "https://169.254.169.254/"})
    )
    respx.get("https://shop.example/track?order=1").mock(
        return_value=httpx.Response(302, headers={"location": "https://169.254.169.254/"})
    )
    api = CustomApiProvider({"url_template": "https://shop.example/api/orders/{order_number}"})
    with pytest.raises(httpx.HTTPStatusError):
        await api.lookup(OrderQuery(order_number="1"))
    page = TrackingPageProvider(
        {"url_template": "https://shop.example/track?order={order_number}"},
        ScriptedOrderExtractor(),
    )
    assert await page.lookup(OrderQuery(order_number="1")) == []
    assert all(call.request.url.host == "shop.example" for call in respx.calls)


@respx.mock
async def test_providers_refuse_private_destinations(monkeypatch: pytest.MonkeyPatch) -> None:
    route = respx.get(url__regex=r".*").mock(return_value=httpx.Response(200, json={"id": 1}))
    api = CustomApiProvider({"url_template": "https://10.0.0.5/orders/{order_number}"})
    with pytest.raises(UnsafeUrlError):
        await api.lookup(OrderQuery(order_number="1"))
    monkeypatch.setattr("app.netguard.resolve_host", lambda host: ["127.0.0.1"])
    page = TrackingPageProvider(
        {"url_template": "https://internal.example/track?order={order_number}"},
        ScriptedOrderExtractor(),
    )
    with pytest.raises(UnsafeUrlError):
        await page.lookup(OrderQuery(order_number="1"))
    assert route.call_count == 0


@respx.mock
async def test_localhost_is_only_reachable_when_allowed() -> None:
    respx.get("http://localhost:9000/orders/1").mock(
        return_value=httpx.Response(200, json={"id": 1})
    )
    cfg = {"url_template": "http://localhost:9000/orders/{order_number}"}
    with pytest.raises(UnsafeUrlError):
        await CustomApiProvider(cfg).lookup(OrderQuery(order_number="1"))
    [rec] = await CustomApiProvider(cfg, allow_localhost=True).lookup(OrderQuery(order_number="1"))
    assert rec.order_number == "1"


# ---- What the agent sees ---------------------------------------------------------------------


def _rec(**kw: Any) -> OrderRecord:
    base: dict[str, Any] = {
        "source": "csv",
        "source_order_id": "1",
        "order_number": "A1",
        "status": "shipped",
    }
    base.update(kw)
    return OrderRecord(**base)


def test_summary_never_carries_contact_details() -> None:
    rec = _rec(customer_email="z@x.co", customer_name="Zed", customer_phone="+1 555 000 1111")
    summary = rec.summary()
    assert "customer_email" not in summary and "customer_name" not in summary
    assert "customer_phone" not in summary
    assert summary["order_number"] == "A1" and summary["source"] == "CSV import"


def test_belongs_to_matches_email_or_phone_and_passes_contactless_orders() -> None:
    assert _rec(customer_email="Z@x.co").belongs_to("z@x.co", None)
    assert not _rec(customer_email="someone@else.co").belongs_to("z@x.co", None)
    assert not _rec(customer_email="someone@else.co").belongs_to(None, "+15550001111")
    assert _rec(customer_phone="(555) 000-1111").belongs_to(None, "+1 555 000 1111")
    assert not _rec(customer_phone="+15550002222").belongs_to("z@x.co", "+15550001111")
    # Either identifier is enough: the email differs but the phone is the customer's.
    assert _rec(customer_email="old@x.co", customer_phone="+15550001111").belongs_to(
        "z@x.co", "+15550001111"
    )
    # Nothing on the order to contradict the customer.
    assert _rec().belongs_to("z@x.co", None)


def test_customer_summary_reduces_someone_elses_order_to_a_verify_request() -> None:
    mine = _rec(order_number="A1", customer_email="z@x.co", total=10.0)
    theirs = _rec(order_number="B2", source_order_id="2", customer_email="other@x.co", total=99.0)
    result = LookupResult(orders=[mine, theirs], sources_checked=["csv"])
    found = result.customer_summary("z@x.co", None)["found"]
    assert found[0]["order_number"] == "A1" and found[0]["total"] == 10.0
    assert found[1] == {
        "order_number": "B2",
        "status": "found_but_unverified",
        "verify": "ask the customer to confirm the email or phone used on the order",
    }
    # The business's own view is untouched.
    assert result.summary()["found"][1]["total"] == 99.0


def test_mask_source_hides_everything_but_known_plain_keys() -> None:
    masked = mask_source(
        {
            "id": "abc",
            "type": "custom_api",
            "url_template": "https://x.example/{order_number}",
            "headers": {"Authorization": "Bearer s3cret"},
            "mapping": {"status": "state"},
            "list_path": "data",
            "api_key": "k",
            "client_id": "cid",
            "empty": "",
        }
    )
    assert masked["id"] == "abc" and masked["url_template"] == "https://x.example/{order_number}"
    assert masked["mapping"] == {"status": "state"} and masked["list_path"] == "data"
    assert masked["headers"] == masked["api_key"] == masked["client_id"] == "••••"
    assert masked["empty"] == ""
