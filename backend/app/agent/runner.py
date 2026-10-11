"""One agent turn: load state, assess, let the model act through tools, enforce post-conditions."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.prompts import system_prompt
from app.agent.requirements import CANONICAL_LABELS, canonical_key
from app.agent.schemas import CaseAssessment
from app.agent.tools import CaseTools, ToolError, TurnContext, case_snapshot
from app.cases import state as sm
from app.cases.service import enqueue_job, load_business, load_case, record_event, transition_case
from app.channels.registry import ChannelRegistry
from app.clock import Clock
from app.db import SessionFactory
from app.models import (
    Business,
    Case,
    CaseState,
    Direction,
    JobKind,
    Message,
    Requirement,
    RequirementStatus,
    Sender,
)

log = logging.getLogger(__name__)

TRIGGERS = ("inbound_message", "followup_due", "call_transcribed", "manual")
MAX_TURN_ATTEMPTS = 3


class TransientProviderError(RuntimeError):
    """The model provider is temporarily unavailable; the job should retry later."""


def is_transient_provider_error(exc: BaseException) -> bool:
    """429s, 5xx, timeouts, and connection failures are worth retrying; everything else is not."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and (status == 429 or status >= 500):
        return True
    name = type(exc).__name__
    return name in {
        "RateLimitError",
        "APIConnectionError",
        "APITimeoutError",
        "InternalServerError",
        "ConnectError",
        "ReadTimeout",
        "TimeoutException",
    }


class AgentRunner(Protocol):
    name: str

    async def run_turn(self, ctx: TurnContext, tools: CaseTools, system: str) -> dict[str, Any]: ...


class Assessor(Protocol):
    name: str

    async def assess(
        self, business: Business, case: Case, snapshot: dict[str, Any]
    ) -> CaseAssessment | None: ...


@dataclass
class TurnResult:
    case_id: uuid.UUID
    trigger: str
    skipped: str | None = None
    terminal_tool: str | None = None
    messages_sent: int = 0
    tool_log: list[dict[str, Any]] = field(default_factory=list)
    provider_meta: dict[str, Any] = field(default_factory=dict)
    final_state: str | None = None


async def apply_assessment(session: AsyncSession, case: Case, assessment: CaseAssessment) -> None:
    case.category = assessment.category
    case.subject = (assessment.subject or case.subject or "")[:500] or None
    case.summary = assessment.summary
    case.sentiment = assessment.sentiment
    case.urgency = assessment.urgency
    case.remedy_requested = assessment.remedy_requested[:500]
    by_key = {r.key: r for r in case.requirements}
    for spec in assessment.required:
        key = canonical_key(spec.key)
        if not key or key in by_key:
            continue
        label = CANONICAL_LABELS.get(key, spec.label[:200])
        req = Requirement(case_id=case.id, key=key, label=label, why_needed=spec.why_needed)
        session.add(req)
        case.requirements.append(req)
        by_key[key] = req
    last_inbound = next(
        (m for m in reversed(case.messages) if m.direction == Direction.INBOUND), None
    )
    for item in assessment.extracted:
        key = canonical_key(item.key)
        if not item.value.strip() or not key:
            continue
        found = by_key.get(key)
        if found is None:
            # A fact the customer already gave is still a requirement, just a satisfied one.
            found = Requirement(
                case_id=case.id,
                key=key,
                label=CANONICAL_LABELS.get(key, key.replace("_", " ").capitalize()),
                why_needed="Provided by the customer",
            )
            session.add(found)
            case.requirements.append(found)
            by_key[key] = found
        found.status = RequirementStatus.PROVIDED
        found.value = item.value.strip()
        if last_inbound is not None:
            found.provided_message_id = last_inbound.id
    await session.flush()


async def run_agent_turn(
    *,
    session_factory: SessionFactory,
    case_id: uuid.UUID,
    trigger: str,
    runner: AgentRunner,
    assessor: Assessor,
    channels: ChannelRegistry,
    clock: Clock,
    orders: Any = None,
    attempt: int = 1,
) -> TurnResult:
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown trigger {trigger!r}")
    result = TurnResult(case_id=case_id, trigger=trigger)
    async with session_factory() as session:
        case = await load_case(session, case_id)
        if case is None:
            result.skipped = "case not found"
            return result
        business = await load_business(session, case.business_id)
        assert business is not None
        customer = case.customer

        if sm.is_terminal(case.state):
            result.skipped = f"case is {case.state}"
            result.final_state = case.state
            return result
        # DB timestamps (not the app clock) decide whether a message arrived mid-turn.
        inbound_times = [m.created_at for m in case.messages if m.direction == Direction.INBOUND]
        last_inbound_at = max(inbound_times) if inbound_times else None

        if trigger == "followup_due" and sm.should_abandon(
            case.followups_sent, business.max_followups
        ):
            await _abandon(session, case, business, now=clock.now())
            await session.commit()
            result.final_state = case.state
            result.terminal_tool = "abandon"
            return result

        if case.state == CaseState.NEW:
            await transition_case(
                session, case, CaseState.GATHERING, actor=Sender.SYSTEM, now=clock.now()
            )

        ctx = TurnContext(
            session=session,
            business=business,
            case=case,
            customer=customer,
            channels=channels,
            clock=clock,
            trigger=trigger,
            orders=orders,
        )
        if trigger in ("inbound_message", "call_transcribed", "manual"):
            try:
                assessment = await assessor.assess(business, case, case_snapshot(ctx))
            except Exception:
                log.exception("assessment failed for case %s", case.id)
                assessment = None
            if assessment is not None:
                await apply_assessment(session, case, assessment)
                await _prefetch_orders(ctx)
                await record_event(
                    session,
                    business_id=business.id,
                    case_id=case.id,
                    type="case_assessed",
                    actor=Sender.AGENT,
                    data={
                        "category": assessment.category,
                        "sentiment": assessment.sentiment,
                        "urgency": assessment.urgency,
                        "required": [r.key for r in assessment.required],
                        "assessor": assessor.name,
                    },
                )

        tools = CaseTools(ctx)
        try:
            result.provider_meta = await runner.run_turn(ctx, tools, system_prompt(business))
        except ToolError as exc:
            log.warning("turn hit a tool limit on case %s: %s", case.id, exc)
            await _escalate_system(tools, reason="tool_limit", detail=str(exc))
        except Exception as exc:
            transient = is_transient_provider_error(exc)
            if transient and attempt < MAX_TURN_ATTEMPTS:
                # Leave the case untouched; the scheduler retries the job with backoff.
                log.warning(
                    "runner %s hit a transient error on case %s (attempt %d): %s",
                    runner.name,
                    case.id,
                    attempt,
                    str(exc)[:200],
                )
                await session.rollback()
                raise TransientProviderError(str(exc)[:500]) from exc
            log.exception("runner %s failed on case %s", runner.name, case.id)
            await record_event(
                session,
                business_id=business.id,
                case_id=case.id,
                type="agent_error",
                actor=Sender.SYSTEM,
                data={"runner": runner.name, "error": str(exc)[:1000], "attempt": attempt},
            )
            if trigger != "followup_due":
                reason = "provider_unavailable" if transient else "agent_error"
                await _escalate_system(tools, reason=reason, detail=str(exc)[:500])

        # Post-conditions: every turn must end in a defined state.
        if ctx.terminal_tool is None and not sm.is_terminal(case.state):
            if ctx.messages_sent > 0 and ctx.messages_failed == ctx.messages_sent:
                # Nothing reached the customer, so a follow-up would nudge them about a
                # message they never saw. A person has to resend it from the dashboard.
                error = next(
                    (
                        m.meta.get("error")
                        for m in reversed(case.messages)
                        if m.direction == Direction.OUTBOUND and m.meta.get("delivery") == "failed"
                    ),
                    None,
                )
                await _escalate_system(
                    tools,
                    reason="delivery_failed",
                    detail=str(error or "unknown error"),
                    summary=f"The reply to the customer could not be delivered on {case.channel}: "
                    f"{error or 'unknown error'}. Fix the channel, then resend the message from "
                    "the case page; the agent takes over again once it is delivered.",
                )
            elif ctx.messages_sent > 0:
                hours = sm.next_followup_hours(
                    business.followup_schedule_hours, case.followups_sent
                )
                await tools.dispatch(
                    "schedule_followup", {"hours": hours, "reason": "default policy after asking"}
                )
            elif trigger == "followup_due":
                # Nothing was sent on a nudge turn; keep the case alive and count the attempt.
                case.followups_sent += 1
                hours = sm.next_followup_hours(
                    business.followup_schedule_hours, case.followups_sent
                )
                await enqueue_job(
                    session,
                    business_id=business.id,
                    kind=JobKind.FOLLOWUP,
                    case_id=case.id,
                    due_at=clock.now() + _hours(hours),
                    payload={"reason": "retry after silent nudge turn"},
                )
            else:
                await _escalate_system(
                    tools, reason="agent_no_action", detail="turn ended without acting"
                )

        result.terminal_tool = ctx.terminal_tool
        result.messages_sent = ctx.messages_sent
        result.tool_log = ctx.log
        result.final_state = case.state
        await session.commit()

        # Messages that arrived while this turn ran need another turn.
        if not sm.is_terminal(case.state) and last_inbound_at is not None:
            newer = await session.execute(
                select(Message.id)
                .where(
                    Message.case_id == case.id,
                    Message.direction == Direction.INBOUND,
                    Message.created_at > last_inbound_at,
                )
                .limit(1)
            )
            if newer.first() is not None:
                await enqueue_job(
                    session,
                    business_id=business.id,
                    kind=JobKind.AGENT_TURN,
                    case_id=case.id,
                    due_at=clock.now(),
                    payload={"trigger": "inbound_message"},
                )
                await session.commit()
    return result


async def _escalate_system(
    tools: CaseTools, *, reason: str, detail: str, summary: str | None = None
) -> None:
    if tools.ctx.terminal_tool is not None:
        return
    await tools.dispatch(
        "escalate",
        {
            "reason": reason,
            "summary_for_human": summary
            or (
                f"The agent could not complete this turn ({reason}): {detail}. "
                "Please review the case timeline and reply to the customer."
            ),
        },
    )


async def _abandon(session: AsyncSession, case: Case, business: Business, *, now: datetime) -> None:
    case.escalation = {
        "reason": "customer_unresponsive",
        "summary": f"Customer did not reply after {case.followups_sent} follow-ups.",
    }
    await transition_case(
        session,
        case,
        CaseState.ABANDONED,
        actor=Sender.SYSTEM,
        now=now,
        data={"reason": "customer_unresponsive", "followups_sent": case.followups_sent},
    )
    await enqueue_job(
        session,
        business_id=business.id,
        kind=JobKind.NOTIFY_ESCALATION,
        case_id=case.id,
        due_at=now,
        payload={"reason": "customer_unresponsive"},
    )


def _hours(h: int) -> Any:
    from datetime import timedelta

    return timedelta(hours=h)


async def _prefetch_orders(ctx: TurnContext) -> None:
    """Match orders before the model acts so it sees them in get_case without a tool call."""
    if ctx.orders is None:
        return
    from app.orders.base import OrderQuery

    number = next(
        (str(r.value) for r in ctx.case.requirements if r.key == "order_number" and r.value),
        None,
    )
    query = OrderQuery(order_number=number, email=ctx.customer.email, phone=ctx.customer.phone)
    try:
        result = await ctx.orders.lookup(ctx.session, ctx.business, query, now=ctx.clock.now())
    except Exception:
        log.exception("order prefetch failed for case %s", ctx.case.id)
        return
    if result.orders or result.errors:
        summary = result.customer_summary(ctx.customer.email, ctx.customer.phone)
        ctx.orders_found = summary["found"]
        await _satisfy_order_number(ctx, summary["found"])
        await record_event(
            ctx.session,
            business_id=ctx.business.id,
            case_id=ctx.case.id,
            type="order_lookup",
            actor=Sender.SYSTEM,
            data={
                "query": query.model_dump(exclude_none=True),
                "found": len(result.orders),
                "sources": summary["sources_checked"],
                "errors": result.errors,
            },
        )


async def _satisfy_order_number(ctx: TurnContext, found: list[dict[str, Any]]) -> None:
    """One verified order for this customer answers the order-number question by itself."""
    verified = [
        o for o in found if o.get("status") != "found_but_unverified" and o.get("order_number")
    ]
    if len(verified) != 1:
        return
    req = next((r for r in ctx.case.requirements if r.key == "order_number"), None)
    if req is None or req.status == RequirementStatus.PROVIDED:
        return
    req.status = RequirementStatus.PROVIDED
    req.value = str(verified[0]["order_number"])
    await ctx.session.flush()
    await record_event(
        ctx.session,
        business_id=ctx.business.id,
        case_id=ctx.case.id,
        type="requirement_satisfied",
        actor=Sender.SYSTEM,
        data={"key": "order_number", "value": req.value, "source": verified[0].get("source")},
    )
