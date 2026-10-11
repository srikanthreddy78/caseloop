from __future__ import annotations

from typing import Any

import pytest
from app.config import Settings
from app.db import scratch_database, sibling_database_url
from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_email_flow import get_case, simulate


def test_sibling_database_url_keeps_host_and_query() -> None:
    url = "postgresql+asyncpg://u:p@db.example:5432/caseloop?ssl=require"
    assert (
        sibling_database_url(url, "scratch")
        == "postgresql+asyncpg://u:p@db.example:5432/caseloop_scratch?ssl=require"
    )
    assert sibling_database_url("postgresql://localhost/caseloop", "test").endswith(
        "/caseloop_test"
    )


async def _business_count(engine: AsyncEngine) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(text("SELECT count(*) FROM businesses"))).scalar_one())


async def test_scratch_database_refuses_existing_businesses(
    settings: Settings, business: dict[str, Any], engine: AsyncEngine
) -> None:
    with pytest.raises(RuntimeError, match="already holds 1 business"):
        async with scratch_database(settings.database_url):
            pass
    # The guard must not have wiped the data it refused to touch.
    assert await _business_count(engine) == 1


async def test_scratch_database_empties_itself_on_exit(
    settings: Settings, client: AsyncClient, engine: AsyncEngine
) -> None:
    async with scratch_database(settings.database_url):
        resp = await client.post("/dev/businesses", json=BUSINESS_PAYLOAD)
        assert resp.status_code == 201
        assert await _business_count(engine) == 1
    assert await _business_count(engine) == 0


async def test_run_jobs_can_be_scoped_to_one_business(
    client: AsyncClient, business: dict[str, Any]
) -> None:
    other = dict(
        BUSINESS_PAYLOAD,
        slug="other",
        inbound_email="help@other-shop.co",
        dial_phone_number="+15550002222",
    )
    resp = await client.post("/dev/businesses", json=other)
    assert resp.status_code == 201
    other_key = resp.json()["data"]["api_key"]
    other_id = resp.json()["data"]["id"]

    mine = await simulate(client, "The lamp arrived cracked.")
    theirs = await simulate(client, "The lamp arrived cracked.", to="help@other-shop.co")

    scoped = await client.post("/dev/run-jobs", json={"business_id": other_id})
    assert scoped.status_code == 200 and scoped.json()["ran"] == 1

    client.headers["Authorization"] = f"Bearer {other_key}"
    assert (await get_case(client, theirs["id"]))["state"] == "awaiting_customer"
    client.headers["Authorization"] = f"Bearer {business['api_key']}"
    assert (await get_case(client, mine["id"]))["state"] == "new"

    everything = await client.post("/dev/run-jobs", json={})
    assert everything.json()["ran"] == 1
    assert (await get_case(client, mine["id"]))["state"] == "awaiting_customer"
