"""Job handlers, registered by kind on the scheduler."""

from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from app.agent.runner import run_agent_turn
from app.cases import state as sm
from app.cases.service import enqueue_job, load_business, load_case, record_event
from app.insights.service import generate_insights
from app.jobs_dial import handle_fetch_call_transcript
from app.models import JobKind, Sender
from app.notifications import notify_escalation
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
        await record_event(
            session,
            business_id=business.id,
            case_id=case.id,
            type="escalation_notified",
            actor=Sender.SYSTEM,
            data={"to": business.escalation_email, "provider_id": provider_id},
        )
        await session.commit()


INSIGHTS_EVERY_HOURS = 24


async def handle_insights(ctx: JobContext) -> None:
    """Refresh the business's insights report, then book the next run."""
    s = ctx.services
    now = ctx.clock.now()
    async with ctx.session_factory() as session:
        business = await load_business(session, ctx.job.business_id)
        if business is None:
            return
        await generate_insights(session, business, now=now, generator=s.insights)
        await enqueue_job(
            session,
            business_id=business.id,
            kind=JobKind.INSIGHTS,
            due_at=now + timedelta(hours=INSIGHTS_EVERY_HOURS),
            payload={"recurring": True},
        )
        await session.commit()


async def handle_deliver_webhook(ctx: JobContext) -> None:
    async with ctx.session_factory() as session:
        business = await load_business(session, ctx.job.business_id)
        if business is None or not business.outbound_webhook_url:
            return
        await deliver_business_webhook(business, ctx.job.payload, now=ctx.clock.now())


async def ensure_daily_insights_jobs(session_factory: Any, now: Any) -> int:
    """Called at startup: every business gets exactly one pending recurring insights job."""
    from sqlalchemy import select

    from app.models import Business, Job, JobStatus

    created = 0
    async with session_factory() as session:
        businesses = (await session.execute(select(Business.id))).scalars().all()
        for business_id in businesses:
            pending = await session.execute(
                select(Job.id)
                .where(
                    Job.business_id == business_id,
                    Job.kind == JobKind.INSIGHTS,
                    Job.status == JobStatus.PENDING,
                )
                .limit(1)
            )
            if pending.first() is None:
                await enqueue_job(
                    session,
                    business_id=business_id,
                    kind=JobKind.INSIGHTS,
                    due_at=now + timedelta(hours=INSIGHTS_EVERY_HOURS),
                    payload={"recurring": True},
                )
                created += 1
        await session.commit()
    return created


def register_handlers(scheduler: Scheduler) -> None:
    scheduler.register(JobKind.AGENT_TURN, handle_agent_turn)
    scheduler.register(JobKind.FOLLOWUP, handle_followup)
    scheduler.register(JobKind.NOTIFY_ESCALATION, handle_notify_escalation)
    scheduler.register(JobKind.FETCH_CALL_TRANSCRIPT, handle_fetch_call_transcript)
    scheduler.register(JobKind.INSIGHTS, handle_insights)
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
