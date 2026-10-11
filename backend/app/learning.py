"""The agent learns from the business: human decisions become lessons, past cases become precedents."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, Case, CaseState, Lesson

MAX_LESSONS_IN_PROMPT = 12
MAX_PRECEDENTS = 4


async def record_lesson(
    session: AsyncSession,
    business: Business,
    *,
    kind: str,
    text: str,
    case: Case | None = None,
) -> Lesson:
    lesson = Lesson(
        business_id=business.id,
        case_id=case.id if case else None,
        kind=kind,
        category=case.category if case else None,
        text=text.strip()[:1000],
    )
    session.add(lesson)
    await session.flush()
    return lesson


def lesson_from_human_resolution(case: Case, resolution_type: str, notes: str) -> str:
    base = f"A person resolved a {case.category or 'support'} case"
    if case.subject:
        base += f' ("{case.subject[:80]}")'
    came_from = " that the agent had escalated" if case.escalation else ""
    text = f"{base}{came_from} as {resolution_type.replace('_', ' ')}."
    if notes.strip():
        text += f" Their note: {notes.strip()[:300]}"
    return text


def lesson_from_human_escalation(case: Case, reason: str, summary: str) -> str:
    text = f"A person took over a {case.category or 'support'} case"
    if case.subject:
        text += f' ("{case.subject[:80]}")'
    text += f" because: {reason.replace('_', ' ')}."
    if summary.strip():
        text += f" {summary.strip()[:300]}"
    return text


def lesson_from_human_reply(case: Case, reply: str) -> str:
    return (
        f"On a {case.category or 'support'} case"
        + (f' ("{case.subject[:80]}")' if case.subject else "")
        + f', a person replied to the customer: "{reply.strip()[:300]}". Match this tone and stance in similar cases.'
    )


async def active_lessons(
    session: AsyncSession, business_id: uuid.UUID, limit: int = MAX_LESSONS_IN_PROMPT
) -> list[Lesson]:
    stmt = (
        select(Lesson)
        .where(Lesson.business_id == business_id, Lesson.active.is_(True))
        .order_by(Lesson.created_at.desc())
        .limit(limit)
    )
    return list((await session.execute(stmt)).scalars().all())


async def precedents(
    session: AsyncSession,
    business_id: uuid.UUID,
    category: str | None,
    *,
    exclude_case_id: uuid.UUID | None = None,
) -> list[dict[str, Any]]:
    """The business's most recent resolved cases of the same kind, as short worked examples."""
    stmt = (
        select(Case)
        .where(Case.business_id == business_id, Case.state == CaseState.RESOLVED)
        .order_by(Case.closed_at.desc())
        .limit(40)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    out: list[dict[str, Any]] = []
    for c in rows:
        if exclude_case_id and c.id == exclude_case_id:
            continue
        if category and c.category != category:
            continue
        res = c.resolution or {}
        out.append(
            {
                "subject": (c.subject or "")[:80],
                "summary": (c.summary or "")[:200],
                "resolution": res.get("type"),
                "notes": str(res.get("notes") or "")[:200],
                "by": res.get("by", "agent"),
            }
        )
        if len(out) >= MAX_PRECEDENTS:
            break
    return out


def learning_block(lessons: list[Lesson], examples: list[dict[str, Any]]) -> str:
    """Prompt section built from what the business taught the agent. Empty when nothing is known."""
    parts: list[str] = []
    if examples:
        parts.append("How this business has handled similar cases (most recent first):")
        for e in examples:
            line = f"- {e['subject'] or e['summary'] or 'case'} -> {str(e['resolution'] or 'resolved').replace('_', ' ')}"
            if e["by"] == "human":
                line += " (decided by a person)"
            if e["notes"]:
                line += f": {e['notes']}"
            parts.append(line)
    if lessons:
        parts.append("Lessons from the business's own people (follow these over your defaults):")
        parts.extend(f"- {lesson.text}" for lesson in lessons)
    return "\n".join(parts)
