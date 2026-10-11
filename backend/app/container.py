"""Service container shared by the app, the scheduler, and tests. Built once per process."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.fake_runner import ScriptedAgentRunner, ScriptedAssessor
from app.agent.openai_runner import OpenAIAgentRunner, OpenAIAssessor
from app.agent.runner import AgentRunner, Assessor
from app.channels.registry import ChannelRegistry
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db import SessionFactory, make_engine, make_session_factory
from app.notifications import FakeNotifier, Notifier
from app.scheduler import Scheduler

log = logging.getLogger(__name__)


@dataclass
class Services:
    settings: Settings
    engine: AsyncEngine
    session_factory: SessionFactory
    clock: Clock
    channels: ChannelRegistry
    runner: AgentRunner
    assessor: Assessor
    scheduler: Scheduler


def build_agent(settings: Settings) -> tuple[AgentRunner, Assessor]:
    if settings.agent_runner == "openai":
        if not settings.openai_api_key:
            log.warning(
                "CASELOOP_AGENT_RUNNER=openai but OPENAI_API_KEY is unset; using scripted agent"
            )
        else:
            client = AsyncOpenAI(api_key=settings.openai_api_key)
            return (
                OpenAIAgentRunner(client, settings.openai_model),
                OpenAIAssessor(client, settings.openai_model, data_dir=settings.data_dir),
            )
    return ScriptedAgentRunner(), ScriptedAssessor()


def build_channels(settings: Settings) -> ChannelRegistry:
    """Real adapters when their keys exist, fakes otherwise. Later milestones fill this in."""
    registry = ChannelRegistry.all_fake()
    if not settings.resend_api_key:
        log.warning("RESEND_API_KEY unset: email is recorded, not delivered")
    if registry.notifier is None:
        registry.notifier = FakeNotifier()
    return registry


def build_services(
    settings: Settings,
    *,
    clock: Clock | None = None,
    channels: ChannelRegistry | None = None,
    runner: AgentRunner | None = None,
    assessor: Assessor | None = None,
    engine: AsyncEngine | None = None,
    notifier: Notifier | None = None,
) -> Services:
    from app.jobs import register_handlers

    engine = engine or make_engine(settings.database_url)
    session_factory = make_session_factory(engine)
    clock = clock or SystemClock()
    channels = channels or build_channels(settings)
    if notifier is not None:
        channels.notifier = notifier
    if runner is None or assessor is None:
        default_runner, default_assessor = build_agent(settings)
        runner = runner or default_runner
        assessor = assessor or default_assessor
    services = Services(
        settings=settings,
        engine=engine,
        session_factory=session_factory,
        clock=clock,
        channels=channels,
        runner=runner,
        assessor=assessor,
        scheduler=None,  # type: ignore[arg-type]
    )
    services.scheduler = Scheduler(session_factory, clock, services)
    register_handlers(services.scheduler)
    return services
