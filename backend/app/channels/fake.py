from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.channels.base import SendResult
from app.models import Business, Case, Customer


@dataclass
class SentRecord:
    channel: str
    case_id: uuid.UUID
    to: str | None
    text: str


@dataclass
class FakeAdapter:
    """Records every send instead of delivering it. Used by tests and `make demo`."""

    channel: str
    sent: list[SentRecord] = field(default_factory=list)

    async def send(
        self, *, business: Business, case: Case, customer: Customer, text: str
    ) -> SendResult:
        to = customer.email if self.channel == "email" else customer.phone
        self.sent.append(SentRecord(channel=self.channel, case_id=case.id, to=to, text=text))
        return SendResult(provider_message_id=f"fake-{self.channel}-{uuid.uuid4().hex[:8]}")


@dataclass
class FakeVoiceAdapter:
    calls: list[tuple[uuid.UUID, str]] = field(default_factory=list)

    async def start_call(
        self, *, business: Business, case: Case, customer: Customer, instruction: str
    ) -> str:
        self.calls.append((case.id, instruction))
        return f"fake-call-{uuid.uuid4().hex[:8]}"
