"""Business-facing notifications: escalation packages, Slack alerts, and the daily digest."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any, Protocol

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.service import case_timeline
from app.models import TERMINAL_STATES, Business, Case, CaseState
from app.netguard import assert_public_url

log = logging.getLogger(__name__)

SLACK_TIMEOUT_SECONDS = 10
DIGEST_LONGEST_WAITING = 5
DIGEST_NEEDS_PERSON_DAYS = 14
NEEDS_PERSON_STATES = (CaseState.ESCALATED, CaseState.ABANDONED)


class Notifier(Protocol):
    async def send_email(
        self, *, to: str, subject: str, text: str, from_email: str | None = None
    ) -> str | None: ...


class FakeNotifier:
    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send_email(
        self, *, to: str, subject: str, text: str, from_email: str | None = None
    ) -> str | None:
        self.sent.append({"to": to, "subject": subject, "text": text, "from": from_email})
        return f"fake-notify-{len(self.sent)}"


async def build_escalation_package(
    session: AsyncSession, case: Case, base_url: str
) -> tuple[str, str]:
    esc = case.escalation or {}
    reqs = (
        "\n".join(
            f"  - {r.label}: {r.status}" + (f" ({r.value})" if r.value else "")
            for r in case.requirements
        )
        or "  (none)"
    )
    timeline = await case_timeline(session, case.id)
    lines = []
    for item in timeline[-25:]:
        if item["kind"] == "message":
            lines.append(
                f"[{item['at'][:16]}] {item['sender']} ({item['direction']}): {item['text'][:300]}"
            )
        elif item["type"] in ("state_changed", "case_assessed", "followup_scheduled"):
            lines.append(f"[{item['at'][:16]}] {item['type']}: {item['data']}")
    subject = (
        f"[Caseloop] Needs a human: {case.subject or case.category or 'case'} ({str(case.id)[:8]})"
    )
    text = (
        f"Case {case.id}\n"
        f"State: {case.state}\nCategory: {case.category}\nSentiment: {case.sentiment}  Urgency: {case.urgency}\n"
        f"Customer: {case.customer.name or ''} {case.customer.email or ''} {case.customer.phone or ''}\n\n"
        f"Why it needs you: {esc.get('reason', 'unknown')}\n{esc.get('summary', '')}\n\n"
        f"Summary: {case.summary or '(not assessed)'}\n\n"
        f"Requirements:\n{reqs}\n\n"
        f"Recent timeline:\n" + "\n".join(lines) + "\n\n"
        f"Open in dashboard: {base_url}/dashboard/cases/{case.id}\n"
    )
    return subject, text


async def notify_escalation(
    session: AsyncSession, notifier: Notifier | None, business: Business, case: Case, base_url: str
) -> str | None:
    subject, text = await build_escalation_package(session, case, base_url)
    if notifier is None:
        return None
    try:
        return await notifier.send_email(
            to=business.escalation_email, subject=subject, text=text, from_email=business.from_email
        )
    except Exception as exc:  # the case is already escalated; a failed email must not fail the job
        log.warning("escalation email to %s could not be sent: %s", business.escalation_email, exc)
        return None


async def post_slack_alert(business: Business, case: Case, base_url: str) -> bool:
    """One short Slack message per escalation, if the business set a webhook URL.

    Delivery problems are logged, never raised: the email package is the record of truth.
    """
    url = (business.settings or {}).get("slack_webhook_url")
    if not url:
        return False
    esc = case.escalation or {}
    text = (
        f"*Needs a human:* {case.subject or case.category or 'case'}\n"
        f"Reason: {esc.get('reason', 'unknown')}  |  Customer: {_who(case)}\n"
        f"{base_url}/dashboard/cases/{case.id}"
    )
    try:
        await asyncio.to_thread(assert_public_url, url)
        async with httpx.AsyncClient(timeout=SLACK_TIMEOUT_SECONDS, follow_redirects=False) as http:
            resp = await http.post(url, json={"text": text})
            resp.raise_for_status()
    except Exception as exc:
        log.warning("slack alert failed for case %s: %s", case.id, exc)
        return False
    return True


async def build_digest(
    session: AsyncSession, business: Business, *, now: datetime, base_url: str
) -> tuple[str, str] | None:
    """Daily summary, or None when nothing is open and nothing is waiting on a person."""
    open_states = [s.value for s in CaseState if s not in TERMINAL_STATES]
    person_states = [s.value for s in NEEDS_PERSON_STATES]
    # Hand-offs older than the window are stale: either dealt with outside Caseloop or not
    # going to be, and repeating them daily only buries today's.
    since = now - timedelta(days=DIGEST_NEEDS_PERSON_DAYS)
    rows = (
        (
            await session.execute(
                select(Case).where(
                    Case.business_id == business.id,
                    or_(
                        Case.state.in_(open_states),
                        and_(Case.state.in_(person_states), Case.closed_at >= since),
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    open_cases = [c for c in rows if c.state not in TERMINAL_STATES]
    needs_person = [c for c in rows if c.state in NEEDS_PERSON_STATES]
    if not open_cases and not needs_person:
        return None

    def line(case: Case) -> str:
        title = case.subject or case.category or "case"
        return f"  - {title} ({_who(case)}): {base_url}/dashboard/cases/{case.id}"

    by_state = "\n".join(
        f"  {state.value}: {sum(1 for c in open_cases if c.state == state)}"
        for state in (CaseState.NEW, CaseState.GATHERING, CaseState.AWAITING_CUSTOMER)
    )
    waiting = sorted(
        (c for c in open_cases if c.state == CaseState.AWAITING_CUSTOMER),
        key=lambda c: c.last_agent_message_at or c.created_at,
    )[:DIGEST_LONGEST_WAITING]
    waiting_lines = "\n".join(
        f"{line(c)}  (asked {_days(now, c.last_agent_message_at or c.created_at)}d ago)"
        for c in waiting
    )
    subject = f"[Caseloop] Daily digest: {len(open_cases)} open, {len(needs_person)} need a person"
    text = (
        f"{business.name}, {now:%d %b %Y}\n\n"
        f"Open cases: {len(open_cases)}\n{by_state}\n\n"
        f"Waiting on the customer longest:\n{waiting_lines or '  (none)'}\n\n"
        f"Need a person ({len(needs_person)}):\n"
        + ("\n".join(line(c) for c in needs_person) or "  (none)")
        + f"\n\nAll cases: {base_url}/dashboard\n"
    )
    return subject, text


def _who(case: Case) -> str:
    return case.customer.name or case.customer.email or case.customer.phone or "unknown customer"


def _days(now: datetime, since: datetime) -> int:
    return max(0, int((now - since).total_seconds() // 86400))
