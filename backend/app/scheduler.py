"""Postgres-backed job queue: one table, one polling loop, handlers registered by kind."""

from __future__ import annotations

import asyncio
import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.db import SessionFactory
from app.models import Job, JobStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
STALE_RUNNING_MINUTES = 10


@dataclass
class JobContext:
    job: Job
    session_factory: SessionFactory
    clock: Clock
    services: Any  # app.main.Services; typed loosely to avoid an import cycle


Handler = Callable[[JobContext], Awaitable[None]]


class Scheduler:
    def __init__(self, session_factory: SessionFactory, clock: Clock, services: Any) -> None:
        self.session_factory = session_factory
        self.clock = clock
        self.services = services
        self.handlers: dict[str, Handler] = {}
        self._stop = asyncio.Event()

    def register(self, kind: str, handler: Handler) -> None:
        self.handlers[kind] = handler

    async def _claim_due(
        self, session: AsyncSession, limit: int, business_id: uuid.UUID | None
    ) -> list[Job]:
        now = self.clock.now()
        stmt = select(Job).where(Job.status == JobStatus.PENDING, Job.due_at <= now)
        if business_id is not None:
            stmt = stmt.where(Job.business_id == business_id)
        stmt = stmt.order_by(Job.due_at).limit(limit).with_for_update(skip_locked=True)
        jobs = list((await session.execute(stmt)).scalars().all())
        for job in jobs:
            job.status = JobStatus.RUNNING
            job.locked_at = now
            job.attempts += 1
        await session.commit()
        return jobs

    async def reclaim_stale(self, session: AsyncSession) -> int:
        """Jobs a dead process left as running go back to pending so they are retried."""
        cutoff = self.clock.now() - timedelta(minutes=STALE_RUNNING_MINUTES)
        jobs = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.status == JobStatus.RUNNING, Job.locked_at < cutoff)
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        for job in jobs:
            await self._finish_run(
                session,
                job,
                {
                    "status": JobStatus.PENDING,
                    "locked_at": None,
                    "last_error": "reclaimed after a stale run",
                },
            )
        await session.commit()
        return len(jobs)

    async def _finish_run(self, session: AsyncSession, job: Job, values: dict[str, Any]) -> None:
        # A late worker must not overwrite a newer attempt or a manual retry.
        stmt = update(Job).where(
            Job.id == job.id,
            Job.status == JobStatus.RUNNING,
            Job.locked_at == job.locked_at,
            Job.attempts == job.attempts,
        )
        try:
            async with session.begin_nested():
                await session.execute(
                    stmt.values(**values).execution_options(synchronize_session=False)
                )
        except IntegrityError as exc:
            if not any(
                name in str(exc.orig)
                for name in (
                    "uq_jobs_single_pending_agent_turn",
                    "uq_jobs_single_pending_recurring",
                )
            ):
                raise
            # Another pending turn reads the latest case; a recurring successor already
            # carries the next run. Retrying this older job would block the whole queue.
            await session.execute(
                stmt.values(
                    status=JobStatus.CANCELLED,
                    locked_at=None,
                    last_error="superseded by an already pending job",
                ).execution_options(synchronize_session=False)
            )

    async def run_due_jobs(self, limit: int = 10, business_id: uuid.UUID | None = None) -> int:
        """Run every due job once (only one business's with business_id). Returns how many ran."""
        async with self.session_factory() as session:
            reclaimed = await self.reclaim_stale(session)
            if reclaimed:
                log.warning("reclaimed %d stale running job(s)", reclaimed)
            jobs = await self._claim_due(session, limit, business_id)
        for job in jobs:
            await self._run_one(job)
        return len(jobs)

    async def _run_one(self, job: Job) -> None:
        handler = self.handlers.get(job.kind)
        ctx = JobContext(
            job=job, session_factory=self.session_factory, clock=self.clock, services=self.services
        )
        error: str | None = None
        if handler is None:
            error = f"no handler for job kind {job.kind!r}"
        else:
            try:
                await handler(ctx)
            except Exception:
                error = traceback.format_exc()
                log.exception("job %s (%s) failed", job.id, job.kind)
        async with self.session_factory() as session:
            if error is None:
                values: dict[str, Any] = {"status": JobStatus.DONE, "last_error": None}
            elif job.attempts >= MAX_ATTEMPTS:
                values = {"status": JobStatus.FAILED, "last_error": error[-4000:]}
            else:
                delay = timedelta(minutes=min(2**job.attempts, 30))
                values = {
                    "status": JobStatus.PENDING,
                    "last_error": error[-4000:],
                    "due_at": self.clock.now() + delay,
                    "locked_at": None,
                }
            values["locked_at"] = None
            await self._finish_run(session, job, values)
            await session.commit()

    async def run_forever(self, poll_seconds: float) -> None:
        log.info("scheduler loop started (poll every %.1fs)", poll_seconds)
        while not self._stop.is_set():
            try:
                ran = await self.run_due_jobs()
            except Exception:
                log.exception("scheduler tick failed")
                ran = 0
            if ran == 0:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass

    def stop(self) -> None:
        self._stop.set()
