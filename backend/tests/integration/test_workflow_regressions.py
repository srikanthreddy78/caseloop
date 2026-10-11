from __future__ import annotations

import uuid
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from app.channels.dial_events import DialEventListener
from app.container import Services
from app.models import Business, Case, Job, JobKind, JobStatus
from app.notifications import FakeNotifier
from httpx import AsyncClient
from sqlalchemy import select

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_auth import sign_in
from tests.integration.test_email_flow import simulate


@pytest.mark.parametrize("state", ["new", "abandoned"])
async def test_human_can_resolve_cases_before_agent_or_after_abandonment(
    auth_client: AsyncClient, services: Services, state: str
) -> None:
    case = await simulate(auth_client, "The lamp arrived cracked.")
    async with services.session_factory() as session:
        row = await session.get(Case, uuid.UUID(case["id"]))
        assert row is not None
        row.state = state
        if state == "abandoned":
            row.closed_at = services.clock.now()
        await session.commit()
    response = await auth_client.post(
        f"/v1/cases/{case['id']}/resolve",
        json={"resolution_type": "other", "notes": "Handled by the team"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["state"] == "resolved"
    assert response.json()["data"]["resolution"]["by"] == "human"


@pytest.mark.parametrize("action", ["resolve", "escalate"])
async def test_human_actions_preserve_outbound_case_webhooks(
    auth_client: AsyncClient, services: Services, action: str
) -> None:
    patched = await auth_client.patch(
        "/v1/businesses/me", json={"outbound_webhook_url": "https://hooks.example.com/cases"}
    )
    assert patched.status_code == 200
    case = await simulate(auth_client, "The lamp arrived cracked.")
    async with services.session_factory() as session:
        row = await session.get(Case, uuid.UUID(case["id"]))
        assert row is not None
        row.state = "gathering"
        await session.commit()
    body = {} if action == "resolve" else {"reason": "taken_by_human", "summary_for_human": "Help"}
    response = await auth_client.post(f"/v1/cases/{case['id']}/{action}", json=body)
    assert response.status_code == 200, response.text
    async with services.session_factory() as session:
        jobs = list((await session.execute(select(Job))).scalars())
    webhooks = [j for j in jobs if j.kind == JobKind.DELIVER_WEBHOOK]
    assert {j.payload["event"] for j in webhooks} == {"case.created", "case.state_changed"}
    assert all(j.status == JobStatus.PENDING for j in webhooks)
    assert all(j.status == JobStatus.CANCELLED for j in jobs if j.kind == JobKind.AGENT_TURN)


async def test_multiple_finished_calls_can_be_queued_for_one_business(
    services: Services, business: dict[str, Any]
) -> None:
    class Dial:
        async def list_calls(self) -> list[SimpleNamespace]:
            return [
                SimpleNamespace(
                    id=f"call_{i}",
                    status="completed",
                    transcript="Customer: the lamp arrived cracked",
                    phone_number_id=BUSINESS_PAYLOAD["dial_number_id"],
                )
                for i in range(2)
            ]

    services.channels.dial = Dial()
    listener = DialEventListener(services)
    assert await listener.sync_recent_calls() == 2
    assert await listener.sync_recent_calls() == 0


@pytest.mark.parametrize("kind", [JobKind.AGENT_TURN, JobKind.INSIGHTS])
async def test_stale_job_with_a_pending_successor_does_not_block_the_queue(
    auth_client: AsyncClient, services: Services, kind: str
) -> None:
    case = await simulate(auth_client, "The lamp arrived cracked.")
    async with services.session_factory() as session:
        stale = (await session.execute(select(Job))).scalar_one()
        stale.kind = kind
        stale.case_id = stale.case_id if kind == JobKind.AGENT_TURN else None
        stale.status = JobStatus.RUNNING
        stale.locked_at = services.clock.now() - timedelta(minutes=30)
        stale.attempts = 1
        await session.flush()
        successor = Job(
            business_id=stale.business_id,
            case_id=stale.case_id,
            kind=kind,
            payload={"trigger": "inbound_message"},
            due_at=services.clock.now(),
        )
        session.add(successor)
        await session.commit()
        stale_id, successor_id = stale.id, successor.id
    assert await services.scheduler.run_due_jobs() == 1
    async with services.session_factory() as session:
        stale = await session.get(Job, stale_id)
        successor = await session.get(Job, successor_id)
        assert stale is not None and stale.status == JobStatus.CANCELLED
        assert successor is not None and successor.status == JobStatus.DONE
        assert await session.get(Case, uuid.UUID(case["id"])) is not None


@pytest.mark.parametrize("job_status", [JobStatus.RUNNING, JobStatus.DONE, JobStatus.CANCELLED])
async def test_retry_rejects_active_or_finished_jobs(
    auth_client: AsyncClient, services: Services, job_status: str
) -> None:
    await simulate(auth_client, "The lamp arrived cracked.")
    async with services.session_factory() as session:
        job = (await session.execute(select(Job))).scalar_one()
        job.status = job_status
        job.locked_at = services.clock.now() if job_status == JobStatus.RUNNING else None
        await session.commit()
        job_id = job.id
    response = await auth_client.post(f"/v1/jobs/{job_id}/retry")
    assert response.status_code == 409, response.text


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("headers", 1),
        ("headers", [1]),
        ("headers", {"Authorization": 1}),
        ("mapping", [1]),
        ("mapping", {"order_number": 1}),
        ("mapping", {"status_map": ["done"]}),
        ("list_path", ["data"]),
    ],
)
async def test_invalid_custom_source_config_is_rejected_before_it_breaks_lookup(
    client: AsyncClient,
    business: dict[str, Any],
    notifier: FakeNotifier,
    field: str,
    value: Any,
) -> None:
    await sign_in(client, notifier, BUSINESS_PAYLOAD["escalation_email"])
    response = await client.put(
        "/v1/orders/sources",
        json={
            "order_sources": [
                {
                    "type": "custom_api",
                    "url_template": "https://shop.example.com/{order_number}",
                    field: value,
                }
            ]
        },
    )
    assert response.status_code == 422, response.text
    sources = await client.get("/v1/orders/sources")
    assert sources.json()["data"]["configured"] == []


async def test_csv_with_bom_imports_and_extra_columns_are_reported(
    auth_client: AsyncClient,
) -> None:
    response = await auth_client.post(
        "/v1/orders/import", json={"csv": "\ufefforder_number,email\n123,jane@example.com\n"}
    )
    assert response.status_code == 201, response.text
    assert response.json()["data"]["imported"] == 1
    malformed = await auth_client.post(
        "/v1/orders/import", json={"csv": "order_number,email\n456,jane@example.com,extra\n"}
    )
    assert malformed.status_code == 422, malformed.text


async def test_invalid_stored_source_does_not_hide_other_order_sources(
    auth_client: AsyncClient, services: Services, business: dict[str, Any]
) -> None:
    pushed = await auth_client.post("/v1/orders", json={"orders": [{"order_number": "123"}]})
    assert pushed.status_code == 201
    async with services.session_factory() as session:
        row = await session.get(Business, uuid.UUID(business["id"]))
        assert row is not None
        row.settings = {"order_sources": [{"type": "custom_api", "headers": 1}]}
        await session.commit()
    response = await auth_client.post("/v1/orders/sources/test", json={"order_number": "123"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["found"][0]["order_number"] == "123"
    assert response.json()["data"]["errors"]
