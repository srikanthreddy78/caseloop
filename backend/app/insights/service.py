"""Insights: what customers complain about, why, and what to fix. Generated from recent cases."""

from __future__ import annotations

import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Protocol, cast

from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.schemas import InsightsReport, Theme
from app.models import Business, Case, InsightSnapshot

log = logging.getLogger(__name__)

WINDOW_DAYS = 30
MAX_CASES = 300

INSIGHTS_INSTRUCTIONS = """You are a customer-experience analyst for {business_name}. You receive a list of recent
support cases (one JSON object each) and write a short, honest report for the business owner.
Group cases into 3-7 themes ordered by impact, estimate each theme's share of cases, name the likely
root causes (product, fulfilment, policy, communication...), and propose concrete fixes the business
could act on this week. Call out emerging issues (new in the last week) and why cases were escalated.
Reference example_case_ids from the input. Do not invent cases or numbers."""


class InsightsGenerator(Protocol):
    name: str

    async def generate(self, business: Business, rows: list[dict[str, Any]]) -> InsightsReport: ...


def case_row(case: Case, now: datetime) -> dict[str, Any]:
    opened = case.created_at or now
    closed = case.closed_at or now
    return {
        "id": str(case.id),
        "channel": case.channel,
        "state": case.state,
        "category": case.category,
        "subject": case.subject,
        "summary": case.summary,
        "sentiment": case.sentiment,
        "urgency": case.urgency,
        "remedy_requested": case.remedy_requested,
        "resolution": (case.resolution or {}).get("type"),
        "escalation_reason": (case.escalation or {}).get("reason"),
        "followups_sent": case.followups_sent,
        "days_open": round(max((closed - opened).total_seconds(), 0) / 86400, 1),
        "opened_at": opened.isoformat(),
    }


async def recent_cases(session: AsyncSession, business: Business, now: datetime) -> list[Case]:
    since = now - timedelta(days=WINDOW_DAYS)
    stmt = (
        select(Case)
        .where(Case.business_id == business.id, Case.updated_at >= since)
        .order_by(Case.updated_at.desc())
        .limit(MAX_CASES)
    )
    return list((await session.execute(stmt)).scalars().all())


async def generate_insights(
    session: AsyncSession, business: Business, *, now: datetime, generator: InsightsGenerator
) -> InsightSnapshot:
    cases = await recent_cases(session, business, now)
    rows = [case_row(c, now) for c in cases]
    if rows:
        report = await generator.generate(business, rows)
    else:
        report = InsightsReport(
            headline="No cases in the last 30 days.",
            themes=[],
            sentiment_trend="n/a",
            emerging_issues=[],
            top_escalation_reasons=[],
        )
    snapshot = InsightSnapshot(
        business_id=business.id,
        period_start=now - timedelta(days=WINDOW_DAYS),
        period_end=now,
        case_count=len(rows),
        model=generator.name,
        report=report.model_dump(),
        generated_at=now,
    )
    session.add(snapshot)
    await session.flush()
    return snapshot


async def latest_snapshot(session: AsyncSession, business: Business) -> InsightSnapshot | None:
    stmt = (
        select(InsightSnapshot)
        .where(InsightSnapshot.business_id == business.id)
        .order_by(InsightSnapshot.generated_at.desc())
        .limit(1)
    )
    return (await session.execute(stmt)).scalar_one_or_none()


class ScriptedInsights:
    """Counts by category; good enough for tests and offline demos."""

    name = "scripted"

    async def generate(self, business: Business, rows: list[dict[str, Any]]) -> InsightsReport:
        total = max(len(rows), 1)
        by_cat = Counter(str(r.get("category") or "other") for r in rows)
        themes = [
            Theme(
                name=f"{cat} issues",
                share_pct=round(100 * n / total),
                example_case_ids=[r["id"] for r in rows if (r.get("category") or "other") == cat][
                    :3
                ],
                root_causes=[f"{n} cases classified as {cat} in the last {WINDOW_DAYS} days"],
                suggested_fixes=[f"Review the {cat} cases and look for a shared cause"],
            )
            for cat, n in by_cat.most_common(7)
        ]
        angry = sum(1 for r in rows if r.get("sentiment") in ("angry", "frustrated"))
        esc = Counter(str(r["escalation_reason"]) for r in rows if r.get("escalation_reason"))
        return InsightsReport(
            headline=f"{len(rows)} cases; top theme: {themes[0].name if themes else 'none'}",
            themes=themes,
            sentiment_trend=f"{angry} of {len(rows)} cases were angry or frustrated",
            emerging_issues=[],
            top_escalation_reasons=[k for k, _ in esc.most_common(5)],
        )


class OpenAIInsights:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    async def generate(self, business: Business, rows: list[dict[str, Any]]) -> InsightsReport:
        response = await self.client.responses.parse(
            model=self.model,
            instructions=INSIGHTS_INSTRUCTIONS.format(business_name=business.name),
            input=cast(Any, [{"role": "user", "content": json.dumps(rows, default=str)}]),
            text_format=InsightsReport,
            reasoning=cast(Any, {"effort": "medium"}),
            max_output_tokens=6000,
        )
        parsed = response.output_parsed
        if parsed is None:
            raise RuntimeError("insights model returned no parsed output")
        return parsed
