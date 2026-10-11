from __future__ import annotations

import httpx
import respx
from app.clock import FakeClock
from app.container import Services
from app.jobs import DIGEST_KIND, ensure_recurring_jobs
from app.models import Business, Job, JobStatus
from app.notifications import FakeNotifier
from httpx import AsyncClient
from sqlalchemy import select

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_email_flow import get_case, run_jobs, simulate

SLACK_URL = "https://hooks.slack.com/services/T000/B000/secret"


async def test_slack_url_round_trips_through_business_settings(
    auth_client: AsyncClient, services: Services
) -> None:
    me = await auth_client.get("/v1/businesses/me")
    assert me.json()["data"]["slack_webhook_url"] is None

    patched = await auth_client.patch("/v1/businesses/me", json={"slack_webhook_url": SLACK_URL})
    assert patched.status_code == 200, patched.text
    # The URL is a secret: only a mask comes back, and the mask on PATCH keeps what is stored.
    assert patched.json()["data"]["slack_webhook_url"] == "••••"
    kept = await auth_client.patch("/v1/businesses/me", json={"slack_webhook_url": "••••"})
    assert kept.json()["data"]["slack_webhook_url"] == "••••"
    async with services.session_factory() as session:
        stored = (await session.execute(select(Business.settings))).scalar_one()
        assert stored["slack_webhook_url"] == SLACK_URL

    bad = await auth_client.patch(
        "/v1/businesses/me", json={"slack_webhook_url": "http://hooks.slack.com/x"}
    )
    assert bad.status_code == 422

    # Other fields still update, and clearing the URL removes it.
    patched = await auth_client.patch(
        "/v1/businesses/me", json={"slack_webhook_url": "", "max_followups": 2}
    )
    data = patched.json()["data"]
    assert data["slack_webhook_url"] is None and data["max_followups"] == 2


@respx.mock
async def test_escalation_posts_to_slack(auth_client: AsyncClient, notifier: FakeNotifier) -> None:
    await auth_client.patch("/v1/businesses/me", json={"slack_webhook_url": SLACK_URL})
    route = respx.post(SLACK_URL).mock(return_value=httpx.Response(200, text="ok"))

    case = await simulate(auth_client, "Order 9001 is broken and I will get my lawyer involved.")
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "escalated"

    assert len(notifier.sent) == 1  # the email package still goes out first
    assert route.call_count == 1
    body = route.calls.last.request.content.decode()
    assert "legal_threat" in body and "Broken lamp" in body and "Jane" in body
    assert f"http://test/dashboard/cases/{case['id']}" in body

    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    notified = next(t for t in timeline if t.get("type") == "escalation_notified")
    assert notified["data"]["slack"] is True


@respx.mock
async def test_slack_failure_is_logged_not_raised(
    auth_client: AsyncClient, notifier: FakeNotifier
) -> None:
    await auth_client.patch("/v1/businesses/me", json={"slack_webhook_url": SLACK_URL})
    respx.post(SLACK_URL).mock(side_effect=httpx.ConnectError("boom"))

    case = await simulate(auth_client, "I will sue you, order 9002 arrived smashed.")
    await run_jobs(auth_client)

    assert len(notifier.sent) == 1
    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    notified = next(t for t in timeline if t.get("type") == "escalation_notified")
    assert notified["data"]["slack"] is False
    # The job completed on the first attempt: nothing is left pending or failed.
    assert await run_jobs(auth_client) == 0


async def test_escalation_without_slack_url_posts_nothing(auth_client: AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(SLACK_URL)
        await simulate(auth_client, "Order 9003: I'm calling my lawyer.")
        await run_jobs(auth_client)
        assert route.call_count == 0


async def test_daily_digest_summarises_open_cases(
    auth_client: AsyncClient,
    services: Services,
    clock: FakeClock,
    notifier: FakeNotifier,
) -> None:
    waiting = await simulate(auth_client, "Lamp arrived cracked.", from_email="a@customer-mail.co")
    await simulate(auth_client, "Refund for order 1002 please.", from_email="b@customer-mail.co")
    await simulate(
        auth_client, "Order 1003 is broken, lawyer time.", from_email="c@customer-mail.co"
    )
    await run_jobs(auth_client)
    assert len(notifier.sent) == 1  # the escalation package only

    created = await ensure_recurring_jobs(services.session_factory, clock.now())
    assert created == 2  # insights + digest
    assert await ensure_recurring_jobs(services.session_factory, clock.now()) == 0

    assert await run_jobs(auth_client, advance_hours=23) == 0
    await run_jobs(auth_client, advance_hours=2)
    digests = [m for m in notifier.sent if "Daily digest" in m["subject"]]
    assert len(digests) == 1
    digest = digests[0]
    assert digest["to"] == BUSINESS_PAYLOAD["escalation_email"]
    assert digest["subject"] == "[Caseloop] Daily digest: 1 open, 1 need a person"
    assert "awaiting_customer: 1" in digest["text"]
    assert f"http://test/dashboard/cases/{waiting['id']}" in digest["text"]
    assert "(asked 0d ago)" in digest["text"]  # the 24h nudge went out in the same tick
    assert "Need a person (1)" in digest["text"]

    # Re-booked for tomorrow, and the next run sends a second digest.
    await run_jobs(auth_client, advance_hours=24)
    assert len([m for m in notifier.sent if "Daily digest" in m["subject"]]) == 2


async def test_digest_is_skipped_when_nothing_is_open(
    auth_client: AsyncClient, services: Services, clock: FakeClock, notifier: FakeNotifier
) -> None:
    await simulate(auth_client, "Refund for order 2001 please.")
    await run_jobs(auth_client)
    assert notifier.sent == []

    await ensure_recurring_jobs(services.session_factory, clock.now())
    await run_jobs(auth_client, advance_hours=25)
    assert notifier.sent == []

    # The job still recurs so tomorrow's digest goes out once something is open.
    async with services.session_factory() as session:
        pending = await session.execute(
            select(Job.id).where(Job.kind == DIGEST_KIND, Job.status == JobStatus.PENDING)
        )
        assert len(pending.scalars().all()) == 1
