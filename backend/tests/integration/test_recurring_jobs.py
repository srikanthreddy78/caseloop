"""Recurring jobs (insights, digest) survive failures and concurrent startups."""

from __future__ import annotations

import asyncio
from typing import Any

from app.clock import FakeClock
from app.container import Services
from app.jobs import DIGEST_KIND, ensure_recurring_jobs
from app.models import Job, JobStatus
from app.notifications import FakeNotifier
from httpx import AsyncClient
from sqlalchemy import select

from tests.integration.test_email_flow import run_jobs, simulate


async def _pending_recurring(services: Services) -> list[Job]:
    async with services.session_factory() as session:
        rows = await session.execute(
            select(Job).where(Job.status == JobStatus.PENDING, Job.case_id.is_(None))
        )
        return list(rows.scalars().all())


async def test_concurrent_startups_book_each_recurring_job_once(
    business: dict[str, Any], services: Services, clock: FakeClock
) -> None:
    results = await asyncio.gather(
        *(ensure_recurring_jobs(services.session_factory, clock.now()) for _ in range(5))
    )
    assert sum(results) == 2  # insights + digest, no matter how many processes raced
    pending = await _pending_recurring(services)
    assert sorted(j.kind for j in pending) == [DIGEST_KIND, "insights"]


async def test_failed_digest_send_still_books_tomorrows_run(
    auth_client: AsyncClient, services: Services, clock: FakeClock, notifier: FakeNotifier
) -> None:
    await simulate(auth_client, "Lamp arrived cracked.")
    await run_jobs(auth_client)
    await ensure_recurring_jobs(services.session_factory, clock.now())

    class Exploding:
        async def send_email(self, **kwargs: Any) -> str | None:
            raise RuntimeError("smtp down")

    services.channels.notifier = Exploding()  # type: ignore[assignment]
    try:
        assert await run_jobs(auth_client, advance_hours=25) >= 1
    finally:
        services.channels.notifier = notifier

    async with services.session_factory() as session:
        jobs = (await session.execute(select(Job).where(Job.kind == DIGEST_KIND))).scalars().all()
    by_status = {j.status: j for j in jobs}
    assert set(by_status) == {JobStatus.DONE, JobStatus.PENDING}  # not retried, chain intact
    assert by_status[JobStatus.PENDING].due_at > clock.now()

    # The next run goes out normally once the notifier works again.
    await run_jobs(auth_client, advance_hours=24)
    assert any("Daily digest" in m["subject"] for m in notifier.sent)
    assert len(await _pending_recurring(services)) == 2


async def test_digest_drops_handoffs_older_than_two_weeks(
    auth_client: AsyncClient, services: Services, clock: FakeClock, notifier: FakeNotifier
) -> None:
    await simulate(
        auth_client, "Order 1003 is broken, lawyer time.", from_email="c@customer-mail.co"
    )
    await run_jobs(auth_client)
    assert len(notifier.sent) == 1  # escalated and emailed today

    clock.advance(hours=24 * 15)
    await simulate(auth_client, "Lamp arrived cracked.", from_email="a@customer-mail.co")
    await run_jobs(auth_client)
    await ensure_recurring_jobs(services.session_factory, clock.now())
    await run_jobs(auth_client, advance_hours=25)
    digest = next(m for m in notifier.sent if "Daily digest" in m["subject"])
    assert digest["subject"] == "[Caseloop] Daily digest: 1 open, 0 need a person"
    assert "Need a person (0)" in digest["text"]
