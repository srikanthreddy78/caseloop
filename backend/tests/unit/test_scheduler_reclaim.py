from __future__ import annotations

from datetime import timedelta

from app.clock import FakeClock
from app.container import Services
from app.models import Job, JobStatus
from httpx import AsyncClient
from sqlalchemy import select

from tests.integration.test_email_flow import simulate


async def test_stale_running_jobs_are_reclaimed(
    auth_client: AsyncClient, services: Services, clock: FakeClock
) -> None:
    await simulate(auth_client, "The lamp arrived cracked.")
    async with services.session_factory() as session:
        job = (await session.execute(select(Job))).scalars().first()
        assert job is not None
        job.status = JobStatus.RUNNING
        job.locked_at = clock.now() - timedelta(minutes=30)
        await session.commit()
        job_id = job.id

    ran = await services.scheduler.run_due_jobs()
    assert ran == 1
    async with services.session_factory() as session:
        job = (await session.execute(select(Job).where(Job.id == job_id))).scalar_one()
        assert job.status == JobStatus.DONE
        assert job.last_error is None
