from __future__ import annotations

import httpx
import pytest
import respx
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
        ("Fulfilled", "delivered"),
        ("in transit", "shipped"),
        ("weird", "unknown"),
        (None, "unknown"),
    ],
)
def test_status_normalization(raw: str | None, expected: str) -> None:
    assert normalize_status(raw) == expected


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
