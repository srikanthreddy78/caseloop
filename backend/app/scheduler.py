"""Postgres-backed job queue: one table, one polling loop, handlers registered by kind."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import Clock
from app.db import SessionFactory
from app.models import Job, JobStatus

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3


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

    async def _claim_due(self, session: AsyncSession, limit: int) -> list[Job]:
        now = self.clock.now()
        stmt = (
            select(Job)
            .where(Job.status == JobStatus.PENDING, Job.due_at <= now)
            .order_by(Job.due_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        jobs = list((await session.execute(stmt)).scalars().all())
        for job in jobs:
            job.status = JobStatus.RUNNING
            job.locked_at = now
            job.attempts += 1
        await session.commit()
        return jobs

    async def run_due_jobs(self, limit: int = 10) -> int:
        """Run every due job once. Returns how many ran. Safe to call from tests and the demo."""
        async with self.session_factory() as session:
            jobs = await self._claim_due(session, limit)
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
                values = {"status": JobStatus.PENDING, "last_error": error[-4000:]}
            await session.execute(update(Job).where(Job.id == job.id).values(**values))
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
