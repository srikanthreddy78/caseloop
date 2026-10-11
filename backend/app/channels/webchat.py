"""Channels with no delivery step: the stored message row is the delivery (web chat, API)."""

from __future__ import annotations

from app.channels.base import SendResult
from app.models import Business, Case, Customer


class StoreOnlyAdapter:
    def __init__(self, channel: str) -> None:
        self.channel = channel

    async def send(
        self, *, business: Business, case: Case, customer: Customer, text: str
    ) -> SendResult:
        return SendResult(provider_message_id=None)


def WebchatAdapter() -> StoreOnlyAdapter:
    return StoreOnlyAdapter("webchat")
