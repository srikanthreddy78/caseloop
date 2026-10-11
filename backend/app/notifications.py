"""Business-facing notifications (escalation packages). Sent through a plain email sender."""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.cases.service import case_timeline
from app.models import Business, Case


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
    return await notifier.send_email(
        to=business.escalation_email, subject=subject, text=text, from_email=business.from_email
    )
