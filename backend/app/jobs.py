"""Job handlers, registered by kind on the scheduler."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from app.agent.runner import run_agent_turn
from app.cases import state as sm
from app.cases.service import enqueue_job, load_business, load_case, record_event
from app.models import JobKind, Sender
from app.notifications import notify_escalation
from app.scheduler import JobContext, Scheduler

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


def register_handlers(scheduler: Scheduler) -> None:
    scheduler.register(JobKind.AGENT_TURN, handle_agent_turn)
    scheduler.register(JobKind.FOLLOWUP, handle_followup)
    scheduler.register(JobKind.NOTIFY_ESCALATION, handle_notify_escalation)


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
