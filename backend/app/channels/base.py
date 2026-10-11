from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.models import Business, Case, Customer


@dataclass
class SendResult:
    provider_message_id: str | None = None
    thread_ids: dict[str, str] | None = None


class ChannelAdapter(Protocol):
    """One implementation per channel. `send` must not raise for soft failures; return ids on success."""

    channel: str

    async def send(
        self, *, business: Business, case: Case, customer: Customer, text: str
    ) -> SendResult: ...


class VoiceAdapter(Protocol):
    async def start_call(
        self, *, business: Business, case: Case, customer: Customer, instruction: str
    ) -> str: ...


class ChannelUnavailable(RuntimeError):
    pass
