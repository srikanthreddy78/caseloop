from __future__ import annotations

from typing import Any

from app.agent.runner import is_transient_provider_error
from app.clock import FakeClock
from app.container import Services
from app.models import Job, JobStatus
from httpx import AsyncClient
from sqlalchemy import select

from tests.integration.test_email_flow import get_case, run_jobs, simulate


class RateLimited(Exception):
    status_code = 429


class FlakyRunner:
    name = "flaky"

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.calls = 0

    async def run_turn(self, ctx: Any, tools: Any, system: str) -> dict[str, Any]:
        self.calls += 1
        if self.calls <= self.failures:
            raise RateLimited("You have no credits remaining")
        await tools.dispatch(
            "send_message", {"text": "Sorry for the wait. Could you send the order number?"}
        )
        await tools.dispatch("schedule_followup", {"hours": 24, "reason": "asked"})
        return {}


def test_transient_classification() -> None:
    assert is_transient_provider_error(RateLimited())
    assert not is_transient_provider_error(ValueError("bad"))


async def test_transient_error_retries_with_backoff_then_succeeds(
    auth_client: AsyncClient, services: Services, clock: FakeClock
) -> None:
    runner = FlakyRunner(failures=1)
    services.runner = runner  # type: ignore[assignment]
    case = await simulate(auth_client, "Lamp arrived broken.")
    assert await run_jobs(auth_client) == 1
    case = await get_case(auth_client, case["id"])
    assert case["state"] in ("new", "gathering") and not case["escalation"]
    async with services.session_factory() as session:
        job = (await session.execute(select(Job).where(Job.kind == "agent_turn"))).scalar_one()
        assert job.status == JobStatus.PENDING and job.attempts == 1
        assert job.due_at > clock.now()
    assert await run_jobs(auth_client) == 0  # not due yet
    assert await run_jobs(auth_client, advance_hours=1) == 1
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "awaiting_customer" and runner.calls == 2


async def test_persistent_provider_outage_escalates_after_retries(
    auth_client: AsyncClient, services: Services
) -> None:
    services.runner = FlakyRunner(failures=10)  # type: ignore[assignment]
    case = await simulate(auth_client, "Lamp arrived broken.")
    for _ in range(3):
        await run_jobs(auth_client, advance_hours=1)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "escalated"
    assert case["escalation"]["reason"] == "provider_unavailable"


async def test_single_matching_order_satisfies_order_number(auth_client: AsyncClient) -> None:
    await auth_client.post(
        "/v1/orders/import",
        json={
            "csv": "order_number,email,status,delivered_at,total\n41005,ruth@customer-mail.co,delivered,2026-10-05,120.00\n"
        },
    )
    case = await simulate(
        auth_client,
        "I'd like a refund for the floor lamp, can't find the order number.",
        from_email="ruth@customer-mail.co",
    )
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    req = next(r for r in case["requirements"] if r["key"] == "order_number")
    assert req["status"] == "provided" and req["value"] == "41005"
    assert case["state"] == "resolved"
