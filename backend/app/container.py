"""Service container shared by the app, the scheduler, and tests. Built once per process."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from openai import AsyncOpenAI
from sqlalchemy.ext.asyncio import AsyncEngine

from app.agent.fake_runner import ScriptedAgentRunner, ScriptedAssessor
from app.agent.openai_runner import OpenAIAgentRunner, OpenAIAssessor
from app.agent.runner import AgentRunner, Assessor
from app.channels.dial import DialSmsAdapter, DialVoiceAdapter, make_dial_client
from app.channels.email import ResendEmailAdapter
from app.channels.registry import ChannelRegistry
from app.channels.webchat import StoreOnlyAdapter
from app.clock import Clock, SystemClock
from app.config import Settings
from app.db import SessionFactory, make_engine, make_session_factory
from app.insights.service import InsightsGenerator, OpenAIInsights, ScriptedInsights
from app.notifications import FakeNotifier, Notifier
from app.orders.extract import OpenAIOrderExtractor, OrderExtractor, ScriptedOrderExtractor
from app.orders.service import OrderService
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
    insights: InsightsGenerator
    orders: OrderService
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


def build_insights(settings: Settings) -> InsightsGenerator:
    if settings.agent_runner == "openai" and settings.openai_api_key:
        return OpenAIInsights(AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_model)
    return ScriptedInsights()


def build_order_extractor(settings: Settings) -> OrderExtractor:
    if settings.agent_runner == "openai" and settings.openai_api_key:
        return OpenAIOrderExtractor(
            AsyncOpenAI(api_key=settings.openai_api_key), settings.openai_model
        )
    return ScriptedOrderExtractor()


def build_channels(settings: Settings) -> ChannelRegistry:
    """Real adapters when their keys exist, fakes otherwise (recorded, never delivered)."""
    registry = ChannelRegistry.all_fake()
    registry.adapters["webchat"] = StoreOnlyAdapter("webchat")
    registry.adapters["api"] = StoreOnlyAdapter("api")
    if settings.resend_api_key:
        email = ResendEmailAdapter(settings.resend_api_key)
        registry.adapters["email"] = email
        registry.notifier = email
    else:
        log.warning("RESEND_API_KEY unset: email is recorded, not delivered")
        registry.notifier = FakeNotifier()
    if settings.dial_api_key:
        client = make_dial_client(settings.dial_api_key, settings.dial_base_url)
        registry.dial = client
        registry.adapters["sms"] = DialSmsAdapter(client)
        registry.voice = DialVoiceAdapter(client)
    else:
        log.warning("DIAL_API_KEY unset: SMS and calls are recorded, not placed")
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
    insights: InsightsGenerator | None = None,
    orders: OrderService | None = None,
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
        insights=insights or build_insights(settings),
        orders=orders or OrderService(build_order_extractor(settings)),
        scheduler=None,  # type: ignore[arg-type]
    )
    services.scheduler = Scheduler(session_factory, clock, services)
    register_handlers(services.scheduler)
    return services
