"""Business-facing REST API. Every query is scoped to the authenticated business."""

from __future__ import annotations

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.responses import FileResponse
from sqlalchemy import func, select

from app.api.deps import BusinessDep, RateLimitApi, ServicesDep, SessionDep
from app.attachments import case_attachments, resolve_stored
from app.cases import state as sm
from app.cases.service import (
    cancel_pending_jobs,
    case_timeline,
    enqueue_job,
    get_or_create_customer,
    ingest_inbound,
    record_event,
    transition_case,
)
from app.channels.base import ChannelUnavailable
from app.insights.service import generate_insights, latest_snapshot
from app.models import Business, Case, CaseState, Direction, JobKind, Message, Sender
from app.orders.base import OrderQuery
from app.schemas import (
    BusinessOut,
    BusinessUpdate,
    CaseDetailOut,
    CaseOut,
    CreateCaseIn,
    DataOut,
    EscalateIn,
    HumanMessageIn,
    InsightSnapshotOut,
    ListOut,
    Meta,
    ResolveIn,
    TimelineItem,
)

router = APIRouter(prefix="/v1", tags=["public"], dependencies=[RateLimitApi])


async def _case_or_404(session: SessionDep, business: Business, case_id: uuid.UUID) -> Case:
    case = (
        await session.execute(
            select(Case).where(Case.id == case_id, Case.business_id == business.id)
        )
    ).scalar_one_or_none()
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found")
    return case


@router.get("/businesses/me", response_model=DataOut[BusinessOut])
async def get_me(business: BusinessDep) -> DataOut[BusinessOut]:
    return DataOut(data=BusinessOut.model_validate(business))


@router.patch("/businesses/me", response_model=DataOut[BusinessOut])
async def update_me(
    payload: BusinessUpdate, business: BusinessDep, session: SessionDep
) -> DataOut[BusinessOut]:
    changes = payload.model_dump(exclude_unset=True)
    if "allowed_origins" in changes:
        # Lives in the settings JSON; reassign the dict so SQLAlchemy sees the change.
        business.settings = {
            **(business.settings or {}),
            "allowed_origins": changes.pop("allowed_origins") or [],
        }
    for field, value in changes.items():
        setattr(business, field, value)
    await session.commit()
    await session.refresh(business)
    return DataOut(data=BusinessOut.model_validate(business))


@router.get("/cases", response_model=ListOut[CaseOut])
async def list_cases(
    business: BusinessDep,
    session: SessionDep,
    state: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListOut[CaseOut]:
    base = select(Case).where(Case.business_id == business.id)
    if state:
        states = [s for s in state.split(",") if s]
        for s in states:
            if s not in {c.value for c in CaseState}:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"unknown state {s!r}")
        base = base.where(Case.state.in_(states))
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(Case.updated_at.desc(), Case.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListOut(
        data=[CaseOut.model_validate(c) for c in rows],
        meta=Meta(total=int(total), limit=limit, offset=offset),
    )


@router.post("/cases", response_model=DataOut[CaseDetailOut], status_code=status.HTTP_201_CREATED)
async def create_case(
    payload: CreateCaseIn,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
    response: Response,
) -> DataOut[CaseDetailOut]:
    if not payload.customer_email and not payload.customer_phone:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, "customer_email or customer_phone is required"
        )
    customer = await get_or_create_customer(
        session,
        business,
        email=payload.customer_email,
        phone=payload.customer_phone,
        name=payload.customer_name,
    )
    result = await ingest_inbound(
        session,
        business,
        customer,
        channel=payload.channel,
        text=payload.text,
        subject=payload.subject,
        now=services.clock.now(),
        reopen_window_days=services.settings.reopen_window_days,
        meta={"source": "api"},
    )
    if payload.external_ref:
        result.case.external_ref = payload.external_ref
    await session.commit()
    await session.refresh(result.case)
    response.headers["Location"] = f"/v1/cases/{result.case.id}"
    return DataOut(data=CaseDetailOut.model_validate(result.case))


@router.get("/cases/{case_id}", response_model=DataOut[CaseDetailOut])
async def get_case(
    case_id: uuid.UUID, business: BusinessDep, session: SessionDep
) -> DataOut[CaseDetailOut]:
    case = await _case_or_404(session, business, case_id)
    return DataOut(data=CaseDetailOut.model_validate(case))


@router.get("/cases/{case_id}/timeline", response_model=DataOut[list[TimelineItem]])
async def get_timeline(
    case_id: uuid.UUID, business: BusinessDep, session: SessionDep
) -> DataOut[list[TimelineItem]]:
    case = await _case_or_404(session, business, case_id)
    items = await case_timeline(session, case.id)
    return DataOut(data=[TimelineItem.model_validate(i) for i in items])


@router.get("/cases/{case_id}/attachments/{index}", response_class=FileResponse)
async def get_case_attachment(
    case_id: uuid.UUID,
    index: int,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
) -> FileResponse:
    """Stream one of the case's attachments. `index` is the number shown on the timeline."""
    case = await _case_or_404(session, business, case_id)
    entries = case_attachments(case.messages)
    if not 0 <= index < len(entries):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
    _, _, att = entries[index]
    path = resolve_stored(services.settings.data_dir, att)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment file is not available")
    return FileResponse(
        path,
        media_type=str(att.get("content_type") or "application/octet-stream"),
        filename=str(att.get("filename") or path.name),
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


@router.post(
    "/cases/{case_id}/messages",
    response_model=DataOut[CaseDetailOut],
    status_code=status.HTTP_201_CREATED,
)
async def human_reply(
    case_id: uuid.UUID,
    payload: HumanMessageIn,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
) -> DataOut[CaseDetailOut]:
    """A person at the business replies to the customer on the case's channel."""
    case = await _case_or_404(session, business, case_id)
    try:
        adapter = services.channels.get(case.channel)
    except ChannelUnavailable as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    sent = await adapter.send(
        business=business, case=case, customer=case.customer, text=payload.text
    )
    msg = Message(
        case_id=case.id,
        direction=Direction.OUTBOUND,
        channel=case.channel,
        sender=Sender.HUMAN,
        body_text=payload.text,
        provider_message_id=sent.provider_message_id,
        provider_thread_ids=sent.thread_ids or {},
    )
    session.add(msg)
    case.last_agent_message_at = services.clock.now()
    await session.flush()
    await record_event(
        session,
        business_id=business.id,
        case_id=case.id,
        type="message_sent",
        actor=Sender.HUMAN,
        data={"message_id": str(msg.id), "channel": case.channel},
    )
    await session.commit()
    await session.refresh(case)
    return DataOut(data=CaseDetailOut.model_validate(case))


@router.post("/cases/{case_id}/resolve", response_model=DataOut[CaseDetailOut])
async def human_resolve(
    case_id: uuid.UUID,
    payload: ResolveIn,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
) -> DataOut[CaseDetailOut]:
    case = await _case_or_404(session, business, case_id)
    try:
        await transition_case(
            session,
            case,
            CaseState.RESOLVED,
            actor=Sender.HUMAN,
            now=services.clock.now(),
            data={"resolution_type": payload.resolution_type},
        )
    except sm.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    case.resolution = {"type": payload.resolution_type, "notes": payload.notes, "by": "human"}
    await cancel_pending_jobs(session, case.id)
    await session.commit()
    await session.refresh(case)
    return DataOut(data=CaseDetailOut.model_validate(case))


@router.post("/cases/{case_id}/escalate", response_model=DataOut[CaseDetailOut])
async def human_escalate(
    case_id: uuid.UUID,
    payload: EscalateIn,
    business: BusinessDep,
    session: SessionDep,
    services: ServicesDep,
) -> DataOut[CaseDetailOut]:
    case = await _case_or_404(session, business, case_id)
    try:
        await transition_case(
            session,
            case,
            CaseState.ESCALATED,
            actor=Sender.HUMAN,
            now=services.clock.now(),
            data={"reason": payload.reason},
        )
    except sm.InvalidTransition as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    case.escalation = {
        "reason": payload.reason,
        "summary": payload.summary_for_human,
        "by": "human",
    }
    await cancel_pending_jobs(session, case.id)
    await session.commit()
    await session.refresh(case)
    return DataOut(data=CaseDetailOut.model_validate(case))


@router.post("/cases/{case_id}/run-turn", status_code=status.HTTP_202_ACCEPTED)
async def run_turn_now(
    case_id: uuid.UUID, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> dict[str, str]:
    """Ask the agent to look at the case again right now (queued)."""
    case = await _case_or_404(session, business, case_id)
    if sm.is_terminal(case.state):
        raise HTTPException(status.HTTP_409_CONFLICT, f"case is {case.state}")
    job = await enqueue_job(
        session,
        business_id=business.id,
        kind=JobKind.AGENT_TURN,
        case_id=case.id,
        due_at=services.clock.now(),
        payload={"trigger": "manual"},
    )
    await session.commit()
    return {"status": "queued" if job else "already_queued"}


@router.get("/insights/latest", response_model=DataOut[InsightSnapshotOut])
async def get_latest_insights(
    business: BusinessDep, session: SessionDep
) -> DataOut[InsightSnapshotOut]:
    snapshot = await latest_snapshot(session, business)
    if snapshot is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no insights generated yet")
    return DataOut(data=InsightSnapshotOut.model_validate(snapshot))


@router.post(
    "/insights/generate",
    response_model=DataOut[InsightSnapshotOut],
    status_code=status.HTTP_201_CREATED,
)
async def generate_insights_now(
    business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[InsightSnapshotOut]:
    """Build a fresh report from the last 30 days of cases (runs the model synchronously)."""
    snapshot = await generate_insights(
        session, business, now=services.clock.now(), generator=services.insights
    )
    await session.commit()
    await session.refresh(snapshot)
    return DataOut(data=InsightSnapshotOut.model_validate(snapshot))


@router.get("/cases/{case_id}/orders", response_model=DataOut[dict[str, Any]])
async def case_orders(
    case_id: uuid.UUID, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[dict[str, Any]]:
    """Orders matching this case's customer and order number, from every connected source."""
    case = await _case_or_404(session, business, case_id)
    number = next(
        (str(r.value) for r in case.requirements if r.key == "order_number" and r.value), None
    )
    query = OrderQuery(order_number=number, email=case.customer.email, phone=case.customer.phone)
    result = await services.orders.lookup(session, business, query, now=services.clock.now())
    await session.commit()
    return DataOut(data=result.summary())
