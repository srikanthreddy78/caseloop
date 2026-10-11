"""Dev-only endpoints: create businesses, simulate inbound traffic, drive the job queue."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from app.api.deps import ServicesDep, SessionDep
from app.auth import generate_api_key, generate_widget_key, hash_api_key, key_prefix
from app.cases.service import get_or_create_customer, ingest_inbound
from app.clock import FakeClock
from app.models import Business, Channel, MemberRole, TeamMember
from app.orders.email_forward import business_for_forward_address, import_forwarded_email
from app.schemas import (
    BusinessOut,
    CaseDetailOut,
    CreateBusinessIn,
    CreateBusinessOut,
    DataOut,
    RunJobsIn,
    RunJobsOut,
    SimulateEmailIn,
    SimulateSmsIn,
)

router = APIRouter(prefix="/dev", tags=["dev"])


async def create_business(session: SessionDep, payload: CreateBusinessIn) -> tuple[Business, str]:
    api_key = generate_api_key()
    business = Business(
        name=payload.name,
        slug=payload.slug,
        inbound_email=str(payload.inbound_email).lower(),
        from_email=str(payload.from_email or payload.inbound_email).lower(),
        escalation_email=str(payload.escalation_email).lower(),
        api_key_hash=hash_api_key(api_key),
        api_key_prefix=key_prefix(api_key),
        widget_public_key=generate_widget_key(),
        context_text=payload.context_text,
        dial_phone_number=payload.dial_phone_number,
        dial_number_id=payload.dial_number_id,
    )
    session.add(business)
    await session.flush()
    session.add(
        TeamMember(business_id=business.id, email=business.escalation_email, role=MemberRole.OWNER)
    )
    await session.commit()
    await session.refresh(business)
    return business, api_key


@router.post(
    "/businesses", response_model=DataOut[CreateBusinessOut], status_code=status.HTTP_201_CREATED
)
async def dev_create_business(
    payload: CreateBusinessIn, session: SessionDep
) -> DataOut[CreateBusinessOut]:
    existing = await session.execute(
        select(Business.id).where(
            (Business.slug == payload.slug)
            | (Business.inbound_email == str(payload.inbound_email).lower())
        )
    )
    if existing.first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "slug or inbound_email already exists")
    business, api_key = await create_business(session, payload)
    out = CreateBusinessOut(**BusinessOut.model_validate(business).model_dump(), api_key=api_key)
    return DataOut(data=out)


@router.post(
    "/simulate/email", response_model=DataOut[CaseDetailOut], status_code=status.HTTP_202_ACCEPTED
)
async def simulate_email(
    payload: SimulateEmailIn, session: SessionDep, services: ServicesDep
) -> DataOut[CaseDetailOut]:
    """Feed an inbound email through the same path the Resend webhook uses, with no network."""
    business = (
        await session.execute(
            select(Business).where(Business.inbound_email == str(payload.to).lower())
        )
    ).scalar_one_or_none()
    if business is None:
        forward_biz = await business_for_forward_address(session, [str(payload.to).lower()])
        if forward_biz is not None:
            n = await import_forwarded_email(
                session,
                forward_biz,
                payload.text,
                extractor=services.orders.extractor,
                now=services.clock.now(),
            )
            await session.commit()
            raise HTTPException(status.HTTP_200_OK, f"orders imported: {n}")
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no business owns that inbound address")
    customer = await get_or_create_customer(
        session, business, email=str(payload.from_email), name=payload.from_name
    )
    result = await ingest_inbound(
        session,
        business,
        customer,
        channel=Channel.EMAIL,
        text=payload.text,
        subject=payload.subject,
        provider_message_id=payload.message_id
        or f"<sim-{services.clock.now().timestamp():.0f}@caseloop.dev>",
        attachments=payload.attachments,
        now=services.clock.now(),
        reopen_window_days=services.settings.reopen_window_days,
        meta={"source": "simulated"},
    )
    await session.commit()
    await session.refresh(result.case)
    return DataOut(data=CaseDetailOut.model_validate(result.case))


@router.post(
    "/simulate/sms", response_model=DataOut[CaseDetailOut], status_code=status.HTTP_202_ACCEPTED
)
async def simulate_sms(
    payload: SimulateSmsIn, session: SessionDep, services: ServicesDep
) -> DataOut[CaseDetailOut]:
    business = (
        await session.execute(select(Business).where(Business.dial_phone_number == payload.to))
    ).scalar_one_or_none()
    if business is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no business owns that phone number")
    customer = await get_or_create_customer(session, business, phone=payload.from_phone)
    result = await ingest_inbound(
        session,
        business,
        customer,
        channel=Channel.SMS,
        text=payload.body,
        now=services.clock.now(),
        reopen_window_days=services.settings.reopen_window_days,
        meta={"source": "simulated"},
    )
    await session.commit()
    await session.refresh(result.case)
    return DataOut(data=CaseDetailOut.model_validate(result.case))


@router.post("/run-jobs", response_model=RunJobsOut)
async def run_jobs(payload: RunJobsIn, services: ServicesDep) -> RunJobsOut:
    """Run due jobs until the queue is quiet. With a fake clock, optionally advance time first."""
    if payload.advance_hours:
        if not isinstance(services.clock, FakeClock):
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST, "advance_hours needs the fake clock (tests/demo)"
            )
        services.clock.advance(hours=payload.advance_hours)
    total = 0
    for _ in range(payload.max_rounds):
        ran = await services.scheduler.run_due_jobs()
        total += ran
        if ran == 0:
            break
    return RunJobsOut(ran=total, now=services.clock.now() + timedelta(0))
