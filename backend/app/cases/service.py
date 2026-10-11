"""Case lifecycle operations shared by every channel and by the agent tools."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases import state as sm
from app.models import (
    TERMINAL_STATES,
    Business,
    Case,
    CaseState,
    Channel,
    Customer,
    Direction,
    Event,
    Job,
    JobKind,
    JobStatus,
    Message,
    Sender,
)


async def get_or_create_customer(
    session: AsyncSession,
    business: Business,
    *,
    email: str | None = None,
    phone: str | None = None,
    name: str | None = None,
) -> Customer:
    if not email and not phone:
        raise ValueError("customer needs an email or a phone")
    email = email.strip().lower() if email else None
    stmt = select(Customer).where(Customer.business_id == business.id)
    if email:
        stmt = stmt.where(Customer.email == email)
    else:
        stmt = stmt.where(Customer.phone == phone)
    customer = (await session.execute(stmt)).scalar_one_or_none()
    if customer is None:
        customer = Customer(business_id=business.id, email=email, phone=phone, name=name)
        session.add(customer)
        await session.flush()
    else:
        if name and not customer.name:
            customer.name = name
        if phone and not customer.phone:
            customer.phone = phone
        if email and not customer.email:
            customer.email = email
    return customer


async def record_event(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    case_id: uuid.UUID | None,
    type: str,
    actor: str = Sender.SYSTEM,
    data: dict[str, Any] | None = None,
) -> Event:
    event = Event(business_id=business_id, case_id=case_id, type=type, actor=actor, data=data or {})
    session.add(event)
    await session.flush()
    return event


async def transition_case(
    session: AsyncSession,
    case: Case,
    target: str,
    *,
    actor: str,
    now: datetime,
    data: dict[str, Any] | None = None,
) -> None:
    previous = case.state
    case.state = sm.transition(previous, target)
    if case.state in TERMINAL_STATES:
        case.closed_at = now
    elif previous in TERMINAL_STATES:
        case.closed_at = None
    await record_event(
        session,
        business_id=case.business_id,
        case_id=case.id,
        type="state_changed",
        actor=actor,
        data={"from": previous, "to": case.state, **(data or {})},
    )


async def cancel_pending_jobs(
    session: AsyncSession, case_id: uuid.UUID, kinds: tuple[str, ...] | None = None
) -> int:
    stmt = (
        update(Job)
        .where(Job.case_id == case_id, Job.status == JobStatus.PENDING)
        .values(status=JobStatus.CANCELLED)
    )
    if kinds:
        stmt = stmt.where(Job.kind.in_(kinds))
    result = await session.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def enqueue_job(
    session: AsyncSession,
    *,
    business_id: uuid.UUID,
    kind: str,
    due_at: datetime,
    case_id: uuid.UUID | None = None,
    payload: dict[str, Any] | None = None,
) -> Job | None:
    """Insert a job. For agent_turn, at most one pending job per case exists (returns None if so)."""
    if kind == JobKind.AGENT_TURN and case_id is not None:
        existing = await session.execute(
            select(Job.id).where(
                Job.case_id == case_id,
                Job.kind == JobKind.AGENT_TURN,
                Job.status == JobStatus.PENDING,
            )
        )
        if existing.first() is not None:
            return None
    job = Job(
        business_id=business_id,
        case_id=case_id,
        kind=kind,
        due_at=due_at,
        payload=payload or {},
    )
    session.add(job)
    await session.flush()
    return job


async def find_case_for_customer(
    session: AsyncSession,
    business: Business,
    customer: Customer,
    *,
    now: datetime,
    reopen_window_days: int,
) -> tuple[Case | None, bool]:
    """Return (case, needs_reopen). Prefers an open case; else a recently closed one to reopen."""
    open_stmt = (
        select(Case)
        .where(
            Case.business_id == business.id,
            Case.customer_id == customer.id,
            Case.state.not_in([s.value for s in TERMINAL_STATES]),
        )
        .order_by(Case.updated_at.desc())
        .limit(1)
    )
    case = (await session.execute(open_stmt)).scalar_one_or_none()
    if case is not None:
        return case, False
    closed_stmt = (
        select(Case)
        .where(
            Case.business_id == business.id,
            Case.customer_id == customer.id,
            Case.closed_at.is_not(None),
            Case.closed_at >= now - timedelta(days=reopen_window_days),
        )
        .order_by(Case.closed_at.desc())
        .limit(1)
    )
    case = (await session.execute(closed_stmt)).scalar_one_or_none()
    return (case, True) if case is not None else (None, False)


@dataclass
class IngestResult:
    case: Case
    message: Message
    created: bool
    reopened: bool
    job: Job | None


async def ingest_inbound(
    session: AsyncSession,
    business: Business,
    customer: Customer,
    *,
    channel: str,
    text: str,
    now: datetime,
    reopen_window_days: int = 14,
    html: str | None = None,
    subject: str | None = None,
    provider_message_id: str | None = None,
    provider_thread_ids: dict[str, Any] | None = None,
    attachments: list[dict[str, Any]] | None = None,
    meta: dict[str, Any] | None = None,
    sender: str = Sender.CUSTOMER,
    schedule_turn: bool = True,
) -> IngestResult:
    """Attach an inbound message to the right case (creating or reopening) and queue an agent turn."""
    Channel(channel)
    case, needs_reopen = await find_case_for_customer(
        session, business, customer, now=now, reopen_window_days=reopen_window_days
    )
    created = reopened = False
    if case is None:
        case = Case(
            business_id=business.id,
            customer_id=customer.id,
            channel=channel,
            subject=subject,
            state=CaseState.NEW,
        )
        session.add(case)
        await session.flush()
        created = True
        await record_event(
            session,
            business_id=business.id,
            case_id=case.id,
            type="case_created",
            actor=sender,
            data={"channel": channel, "subject": subject},
        )
    elif needs_reopen:
        await transition_case(
            session, case, CaseState.GATHERING, actor=sender, now=now, data={"reason": "reopened"}
        )
        case.followups_sent = 0
        reopened = True
    elif case.state == CaseState.AWAITING_CUSTOMER:
        await cancel_pending_jobs(session, case.id, kinds=(JobKind.FOLLOWUP,))
        await transition_case(session, case, CaseState.GATHERING, actor=sender, now=now)

    message = Message(
        case_id=case.id,
        direction=Direction.INBOUND,
        channel=channel,
        sender=sender,
        body_text=text,
        body_html=html,
        provider_message_id=provider_message_id,
        provider_thread_ids=provider_thread_ids or {},
        attachments=attachments or [],
        meta=meta or {},
    )
    session.add(message)
    case.last_customer_message_at = now
    if provider_message_id and channel == Channel.EMAIL:
        ids = list(case.email_thread_message_ids or [])
        if provider_message_id not in ids:
            ids.append(provider_message_id)
        case.email_thread_message_ids = ids
    await session.flush()
    await record_event(
        session,
        business_id=business.id,
        case_id=case.id,
        type="message_received",
        actor=sender,
        data={"message_id": str(message.id), "channel": channel},
    )
    job = None
    if schedule_turn:
        job = await enqueue_job(
            session,
            business_id=business.id,
            kind=JobKind.AGENT_TURN,
            case_id=case.id,
            due_at=now,
            payload={"trigger": "inbound_message"},
        )
    await session.refresh(case)
    return IngestResult(case=case, message=message, created=created, reopened=reopened, job=job)


async def load_case(session: AsyncSession, case_id: uuid.UUID) -> Case | None:
    return (await session.execute(select(Case).where(Case.id == case_id))).scalar_one_or_none()


async def load_business(session: AsyncSession, business_id: uuid.UUID) -> Business | None:
    return (
        await session.execute(select(Business).where(Business.id == business_id))
    ).scalar_one_or_none()


async def case_timeline(session: AsyncSession, case_id: uuid.UUID) -> list[dict[str, Any]]:
    """Messages and events merged in time order for the dashboard."""
    events = (await session.execute(select(Event).where(Event.case_id == case_id))).scalars().all()
    messages = (
        (await session.execute(select(Message).where(Message.case_id == case_id))).scalars().all()
    )
    items: list[dict[str, Any]] = []
    for e in events:
        items.append(
            {
                "kind": "event",
                "at": e.created_at.isoformat(),
                "type": e.type,
                "actor": e.actor,
                "data": e.data,
            }
        )
    for m in messages:
        items.append(
            {
                "kind": "message",
                "at": m.created_at.isoformat(),
                "direction": m.direction,
                "channel": m.channel,
                "sender": m.sender,
                "text": m.body_text,
                "attachments": m.attachments,
                "meta": m.meta,
            }
        )
    items.sort(key=lambda i: i["at"])
    return items
