"""SMS and AI voice calls over Dial (getdial.ai)."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from dial_sdk import DialClient, DialConfig

from app.channels.base import ChannelUnavailable, SendResult
from app.models import Business, Case, Customer

log = logging.getLogger(__name__)


def make_dial_client(api_key: str, base_url: str | None = None) -> DialClient:
    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return DialClient(DialConfig(**kwargs))


def _params(cls: Any, **kwargs: Any) -> Any:
    """Dial param types are TypedDicts in some versions and dataclasses in others."""
    if isinstance(cls, type) and issubclass(cls, dict):
        return kwargs
    try:
        return cls(**kwargs)
    except TypeError:
        return kwargs


class DialSmsAdapter:
    channel = "sms"

    def __init__(self, client: DialClient) -> None:
        self.client = client

    async def send(
        self, *, business: Business, case: Case, customer: Customer, text: str
    ) -> SendResult:
        if not business.dial_number_id:
            raise ChannelUnavailable("business has no Dial number configured")
        if not customer.phone:
            raise ChannelUnavailable("customer has no phone number")
        from dial_sdk.types import SendMessageParams

        message = await self.client.send_message(
            _params(
                SendMessageParams,
                to=customer.phone,
                from_number_id=business.dial_number_id,
                body=text,
            )
        )
        return SendResult(provider_message_id=getattr(message, "id", None))


class DialVoiceAdapter:
    def __init__(self, client: DialClient, language: str = "en-US") -> None:
        self.client = client
        self.language = language

    async def start_call(
        self, *, business: Business, case: Case, customer: Customer, instruction: str
    ) -> str:
        if not business.dial_number_id:
            raise ChannelUnavailable("business has no Dial number configured")
        if not customer.phone:
            raise ChannelUnavailable("customer has no phone number")
        from dial_sdk.types import MakeCallParams

        call = await self.client.make_call(
            _params(
                MakeCallParams,
                to=customer.phone,
                from_number_id=business.dial_number_id,
                outbound_instruction=instruction,
                language=self.language,
                idempotency_key=f"{case.id}:{uuid.uuid4().hex[:8]}",
            )
        )
        return str(getattr(call, "id", ""))


def call_instruction(
    business: Business, case: Case, customer: Customer, agent_instruction: str
) -> str:
    """System prompt for Dial's voice agent: who it is, why it is calling, what to collect."""
    missing = [r.label for r in case.requirements if r.status == "missing"]
    return (
        f"You are calling on behalf of {business.name}'s support team about the customer's open "
        f"{case.category or 'support'} request"
        + (f": {case.subject}" if case.subject else "")
        + ".\n"
        f"Customer name: {customer.name or 'unknown'}.\n"
        f"Goal: {agent_instruction.strip()}\n"
        + (f"Information still needed: {', '.join(missing)}.\n" if missing else "")
        + "Be brief and polite. Confirm each item you collect by repeating it back. Do not promise "
        "refunds or replacements; say the team will confirm by email. If the customer cannot talk, "
        "offer to follow up by text and end the call."
    )


def transcript_text(call: Any) -> str:
    turns = getattr(call, "transcript_turns", None) or []
    lines = []
    for t in turns:
        speaker = getattr(t, "speaker", None) or (t.get("speaker") if isinstance(t, dict) else "?")
        text = getattr(t, "text", None) or (t.get("text") if isinstance(t, dict) else "")
        lines.append(f"{speaker}: {text}")
    if lines:
        return "\n".join(lines)
    return str(getattr(call, "transcript", "") or "")
