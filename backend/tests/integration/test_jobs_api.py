from __future__ import annotations

import uuid
from typing import Any

from app.container import Services
from app.models import Job, JobStatus
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_chat_flow import run_jobs


async def _enqueue_bogus(services: Services, business_id: str) -> uuid.UUID:
    """A job kind with no handler fails every attempt, so it ends up `failed` after MAX_ATTEMPTS."""
    async with services.session_factory() as session:
        job = Job(
            business_id=uuid.UUID(business_id),
            kind="bogus",
            due_at=services.clock.now(),
            payload={"why": "test"},
        )
        session.add(job)
        await session.commit()
        return job.id


async def test_failed_jobs_are_listed_and_retryable(
    auth_client: AsyncClient, business: dict[str, Any], services: Services
) -> None:
    job_id = await _enqueue_bogus(services, business["id"])
    assert await run_jobs(auth_client) == 3  # MAX_ATTEMPTS rounds, then it gives up

    resp = await auth_client.get("/v1/jobs")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["meta"]["total"] == 1
    (job,) = body["data"]
    assert job["id"] == str(job_id)
    assert job["kind"] == "bogus" and job["status"] == "failed" and job["attempts"] == 3
    assert "no handler for job kind 'bogus'" in job["last_error"]

    assert (await auth_client.get("/v1/jobs", params={"status": "pending"})).json()["data"] == []
    assert (await auth_client.get("/v1/jobs", params={"status": "done"})).status_code == 422

    retry = await auth_client.post(f"/v1/jobs/{job_id}/retry")
    assert retry.status_code == 200, retry.text
    data = retry.json()["data"]
    assert data["status"] == "pending" and data["attempts"] == 0
    assert data["due_at"].startswith(services.clock.now().isoformat()[:19])

    pending = (await auth_client.get("/v1/jobs", params={"status": "pending"})).json()["data"]
    assert [j["id"] for j in pending] == [str(job_id)]
    again = await auth_client.post(f"/v1/jobs/{job_id}/retry")
    assert again.status_code == 409

    # It runs again, fails again, and shows up as failed once more.
    assert await run_jobs(auth_client) == 3
    failed = (await auth_client.get("/v1/jobs")).json()["data"]
    assert [j["status"] for j in failed] == ["failed"]


async def test_jobs_are_scoped_to_the_business(
    auth_client: AsyncClient, business: dict[str, Any], services: Services
) -> None:
    job_id = await _enqueue_bogus(services, business["id"])
    await run_jobs(auth_client)

    other = await auth_client.post(
        "/dev/businesses",
        json={
            **BUSINESS_PAYLOAD,
            "slug": "other",
            "inbound_email": "support@other.caseloop.dev",
            "dial_phone_number": None,
        },
    )
    assert other.status_code == 201, other.text
    headers = {"Authorization": f"Bearer {other.json()['data']['api_key']}"}

    assert (await auth_client.get("/v1/jobs", headers=headers)).json()["meta"]["total"] == 0
    denied = await auth_client.post(f"/v1/jobs/{job_id}/retry", headers=headers)
    assert denied.status_code == 404
    assert denied.json()["error"]["code"] == "not_found"

    async with services.session_factory() as session:
        job = await session.get(Job, job_id)
        assert job is not None and job.status == JobStatus.FAILED
