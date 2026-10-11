from __future__ import annotations

import os
import tempfile
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from app.agent.fake_runner import ScriptedAgentRunner, ScriptedAssessor
from app.channels.fake import FakeAdapter
from app.channels.registry import ChannelRegistry
from app.clock import FakeClock
from app.config import Settings
from app.container import Services, build_services
from app.db import make_engine
from app.main import create_app
from app.models import Base
from app.notifications import FakeNotifier
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


def _test_database_url() -> str:
    url = os.environ.get("CASELOOP_TEST_DATABASE_URL")
    if url:
        return url
    base = Settings().database_url
    name = base.rsplit("/", 1)[-1].split("?", 1)[0]
    return base.replace(f"/{name}", f"/{name}_test")


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings(
        env="test",
        database_url=_test_database_url(),
        agent_runner="scripted",
        scheduler_enabled=False,
        base_url="http://test",
        data_dir=Path(tempfile.mkdtemp(prefix="caseloop-test-")),
    )


@pytest_asyncio.fixture(scope="session")
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = make_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def clean_tables(engine: AsyncEngine) -> AsyncIterator[None]:
    yield
    names = ", ".join(t.name for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def channels() -> ChannelRegistry:
    return ChannelRegistry.all_fake()


@pytest.fixture
def notifier() -> FakeNotifier:
    return FakeNotifier()


@pytest.fixture
def services(
    settings: Settings,
    engine: AsyncEngine,
    clock: FakeClock,
    channels: ChannelRegistry,
    notifier: FakeNotifier,
) -> Services:
    return build_services(
        settings,
        engine=engine,
        clock=clock,
        channels=channels,
        runner=ScriptedAgentRunner(),
        assessor=ScriptedAssessor(),
        notifier=notifier,
    )


@pytest_asyncio.fixture
async def client(services: Services) -> AsyncIterator[AsyncClient]:
    app = create_app(services)
    app.state.services = services
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


BUSINESS_PAYLOAD: dict[str, Any] = {
    "name": "Acme Lamps",
    "slug": "acme",
    "inbound_email": "support@acme.caseloop.dev",
    "escalation_email": "ops@acme-lamps.co",
    "context_text": "Damaged items: replacement after photos. Refunds within 30 days with order number.",
    "dial_phone_number": "+15550001111",
    "dial_number_id": "num_test",
}


@pytest_asyncio.fixture
async def business(client: AsyncClient) -> dict[str, Any]:
    resp = await client.post("/dev/businesses", json=BUSINESS_PAYLOAD)
    assert resp.status_code == 201, resp.text
    return resp.json()["data"]


@pytest_asyncio.fixture
async def auth_client(client: AsyncClient, business: dict[str, Any]) -> AsyncClient:
    client.headers.update({"Authorization": f"Bearer {business['api_key']}"})
    return client


def email_adapter(channels: ChannelRegistry) -> FakeAdapter:
    adapter = channels.adapters["email"]
    assert isinstance(adapter, FakeAdapter)
    return adapter
