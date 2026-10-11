"""Queue visibility for a business: list its jobs by status and retry the ones that failed."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select

from app.api.deps import BusinessDep, RateLimitApi, ServicesDep, SessionDep
from app.models import Job, JobKind, JobStatus
from app.schemas import DataOut, ListOut, Meta

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/jobs", tags=["jobs"], dependencies=[RateLimitApi])

_LISTABLE = {JobStatus.FAILED, JobStatus.PENDING, JobStatus.RUNNING}


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    status: str
    case_id: uuid.UUID | None
    attempts: int
    due_at: datetime
    last_error: str | None
    payload: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=ListOut[JobOut])
async def list_jobs(
    business: BusinessDep,
    session: SessionDep,
    status_: Annotated[str, Query(alias="status")] = "failed",
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ListOut[JobOut]:
    if status_ not in {s.value for s in _LISTABLE}:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            f"status must be one of {sorted(s.value for s in _LISTABLE)}",
        )
    base = select(Job).where(Job.business_id == business.id, Job.status == status_)
    total = (await session.execute(select(func.count()).select_from(base.subquery()))).scalar_one()
    rows = (
        (
            await session.execute(
                base.order_by(Job.updated_at.desc(), Job.id).limit(limit).offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return ListOut(
        data=[JobOut.model_validate(j) for j in rows],
        meta=Meta(total=int(total), limit=limit, offset=offset),
    )


@router.post("/{job_id}/retry", response_model=DataOut[JobOut])
async def retry_job(
    job_id: uuid.UUID, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> DataOut[JobOut]:
    """Put a failed (or stuck) job back on the queue: pending, attempts reset, due now."""
    job = (
        await session.execute(select(Job).where(Job.id == job_id, Job.business_id == business.id))
    ).scalar_one_or_none()
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    if job.status == JobStatus.PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, "job is already pending")
    if job.kind == JobKind.AGENT_TURN and job.case_id is not None:
        # At most one pending agent_turn per case (enforced by a partial unique index).
        clash = await session.execute(
            select(Job.id).where(
                Job.case_id == job.case_id,
                Job.kind == JobKind.AGENT_TURN,
                Job.status == JobStatus.PENDING,
                Job.id != job.id,
            )
        )
        if clash.first() is not None:
            raise HTTPException(status.HTTP_409_CONFLICT, "an agent turn is already queued")
    job.status = JobStatus.PENDING
    job.attempts = 0
    job.due_at = services.clock.now()
    job.locked_at = None
    await session.commit()
    await session.refresh(job)
    log.info(
        "job retried",
        extra={"job_id": str(job.id), "kind": job.kind, "case_id": str(job.case_id)},
    )
    return DataOut(data=JobOut.model_validate(job))
