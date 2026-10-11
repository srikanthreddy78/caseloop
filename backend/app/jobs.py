"""Job handlers, registered by kind on the scheduler."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runner import run_agent_turn
from app.cases import state as sm
from app.cases.service import enqueue_job, load_business, load_case, record_event
from app.db import SessionFactory
from app.insights.service import generate_insights
from app.jobs_dial import handle_fetch_call_transcript
from app.models import Business, Job, JobKind, JobStatus, Sender
from app.notifications import build_digest, notify_escalation, post_slack_alert
from app.scheduler import JobContext, Scheduler
from app.webhooks.outbound import deliver_business_webhook

log = logging.getLogger(__name__)


async def handle_agent_turn(ctx: JobContext) -> None:
    s = ctx.services
    assert ctx.job.case_id is not None
    await run_agent_turn(
        session_factory=ctx.session_factory,
        case_id=ctx.job.case_id,
        trigger=ctx.job.payload.get("trigger", "manual"),
        runner=s.runner,
        assessor=s.assessor,
        channels=s.channels,
        clock=ctx.clock,
        orders=s.orders,
        attempt=ctx.job.attempts,
    )


async def handle_followup(ctx: JobContext) -> None:
    """A follow-up fell due. Either nudge (via an agent turn) or abandon; the turn decides."""
    s = ctx.services
    assert ctx.job.case_id is not None
    async with ctx.session_factory() as session:
        case = await load_case(session, ctx.job.case_id)
        if case is None or sm.is_terminal(case.state):
            return
        if (
            case.last_customer_message_at
            and case.last_agent_message_at
            and case.last_customer_message_at > case.last_agent_message_at
        ):
            # Customer already replied; an inbound turn is (or was) queued. Nothing to nudge.
            return
    await run_agent_turn(
        session_factory=ctx.session_factory,
        case_id=ctx.job.case_id,
        trigger="followup_due",
        runner=s.runner,
        assessor=s.assessor,
        channels=s.channels,
        clock=ctx.clock,
        orders=s.orders,
        attempt=ctx.job.attempts,
    )


async def handle_notify_escalation(ctx: JobContext) -> None:
    s = ctx.services
    assert ctx.job.case_id is not None
    async with ctx.session_factory() as session:
        case = await load_case(session, ctx.job.case_id)
        if case is None:
            return
        business = await load_business(session, case.business_id)
        assert business is not None
        provider_id = await notify_escalation(
            session, s.channels.notifier, business, case, s.settings.base_url
        )
        slack_sent = await post_slack_alert(business, case, s.settings.base_url)
        await record_event(
            session,
            business_id=business.id,
            case_id=case.id,
            type="escalation_notified",
            actor=Sender.SYSTEM,
            data={"to": business.escalation_email, "provider_id": provider_id, "slack": slack_sent},
        )
        await session.commit()


INSIGHTS_EVERY_HOURS = 24
DIGEST_EVERY_HOURS = 24
# Not a JobKind member: Job.kind is a plain string column and this job never touches a case.
DIGEST_KIND = "digest"

RECURRING: tuple[tuple[str, int], ...] = (
    (JobKind.INSIGHTS, INSIGHTS_EVERY_HOURS),
    (DIGEST_KIND, DIGEST_EVERY_HOURS),
)
_RECURRING_HOURS = dict(RECURRING)


async def book_recurring(
    session: AsyncSession, business_id: uuid.UUID, kind: str, due_at: datetime
) -> bool:
    """Insert the next run of a recurring job unless one is already pending for the business.

    The partial unique index ``uq_jobs_single_pending_recurring`` makes this safe when several
    processes start at once: the loser's insert is simply dropped.
    """
    stmt = (
        insert(Job)
        .values(
            id=uuid.uuid4(),
            business_id=business_id,
            case_id=None,
            kind=kind,
            payload={"recurring": True},
            due_at=due_at,
            status=JobStatus.PENDING,
            attempts=0,
        )
        .on_conflict_do_nothing(
            index_elements=["business_id", "kind"],
            index_where=text(
                "status = 'pending' AND case_id IS NULL AND kind IN ('insights', 'digest')"
            ),
        )
    )
    result = await session.execute(stmt)
    return bool(getattr(result, "rowcount", 0))


async def _run_recurring(ctx: JobContext, kind: str, work: Any) -> None:
    """Run one recurring job and always book the next one, even when this run blew up.

    Errors are logged rather than raised: a retry of this run would collide with the run
    already booked for tomorrow, and the next run is the retry.
    """
    now = ctx.clock.now()
    async with ctx.session_factory() as session:
        business = await load_business(session, ctx.job.business_id)
    if business is None:
        return
    try:
        await work(business, now)
    except Exception:
        log.exception("%s job for business %s failed; next run is booked", kind, business.id)
    finally:
        async with ctx.session_factory() as session:
            await book_recurring(
                session, business.id, kind, now + timedelta(hours=_RECURRING_HOURS[kind])
            )
            await session.commit()


async def handle_insights(ctx: JobContext) -> None:
    """Refresh the business's insights report, then book the next run."""
    s = ctx.services

    async def work(business: Business, now: datetime) -> None:
        async with ctx.session_factory() as session:
            business = (await load_business(session, business.id)) or business
            await generate_insights(session, business, now=now, generator=s.insights)
            await session.commit()

    await _run_recurring(ctx, JobKind.INSIGHTS, work)


async def handle_digest(ctx: JobContext) -> None:
    """Email the business a one-screen summary of what is open, then book tomorrow's."""
    s = ctx.services

    async def work(business: Business, now: datetime) -> None:
        async with ctx.session_factory() as session:
            digest = await build_digest(session, business, now=now, base_url=s.settings.base_url)
        if digest is not None and s.channels.notifier is not None:
            subject, text = digest
            await s.channels.notifier.send_email(
                to=business.escalation_email,
                subject=subject,
                text=text,
                from_email=business.from_email,
            )

    await _run_recurring(ctx, DIGEST_KIND, work)


async def handle_deliver_webhook(ctx: JobContext) -> None:
    async with ctx.session_factory() as session:
        business = await load_business(session, ctx.job.business_id)
        if business is None or not business.outbound_webhook_url:
            return
        await deliver_business_webhook(business, ctx.job.payload, now=ctx.clock.now())


async def ensure_recurring_jobs(session_factory: SessionFactory, now: datetime) -> int:
    """Called at startup: every business gets exactly one pending job per recurring kind."""
    created = 0
    async with session_factory() as session:
        businesses = (await session.execute(select(Business.id))).scalars().all()
        for business_id in businesses:
            for kind, every_hours in RECURRING:
                if await book_recurring(
                    session, business_id, kind, now + timedelta(hours=every_hours)
                ):
                    created += 1
        await session.commit()
    return created


# main.py still imports the old name.
ensure_daily_insights_jobs = ensure_recurring_jobs


def register_handlers(scheduler: Scheduler) -> None:
    scheduler.register(JobKind.AGENT_TURN, handle_agent_turn)
    scheduler.register(JobKind.FOLLOWUP, handle_followup)
    scheduler.register(JobKind.NOTIFY_ESCALATION, handle_notify_escalation)
    scheduler.register(JobKind.FETCH_CALL_TRANSCRIPT, handle_fetch_call_transcript)
    scheduler.register(JobKind.INSIGHTS, handle_insights)
    scheduler.register(DIGEST_KIND, handle_digest)
    scheduler.register(JobKind.DELIVER_WEBHOOK, handle_deliver_webhook)


async def enqueue_manual_turn(
    ctx_session: Any, business_id: uuid.UUID, case_id: uuid.UUID, now: Any
) -> Any:
    return await enqueue_job(
        ctx_session,
        business_id=business_id,
        kind=JobKind.AGENT_TURN,
        case_id=case_id,
        due_at=now,
        payload={"trigger": "manual"},
    )
