"""API request/response schemas."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, EmailStr, Field, field_validator, model_validator


class ErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ErrorOut(BaseModel):
    error: ErrorBody


class CustomerOut(BaseModel):
    id: uuid.UUID
    email: str | None
    phone: str | None
    name: str | None
    model_config = {"from_attributes": True}


class RequirementOut(BaseModel):
    key: str
    label: str
    why_needed: str
    status: str
    value: Any | None
    asked_count: int
    model_config = {"from_attributes": True}


class MessageOut(BaseModel):
    id: uuid.UUID
    direction: str
    channel: str
    sender: str
    body_text: str
    attachments: list[dict[str, Any]]
    meta: dict[str, Any]
    created_at: datetime
    model_config = {"from_attributes": True}


class CaseOut(BaseModel):
    id: uuid.UUID
    state: str
    channel: str
    category: str | None
    subject: str | None
    summary: str | None
    sentiment: str | None
    urgency: str | None
    remedy_requested: str | None
    external_ref: str | None
    followups_sent: int
    last_customer_message_at: datetime | None
    last_agent_message_at: datetime | None
    resolution: dict[str, Any] | None
    escalation: dict[str, Any] | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    customer: CustomerOut
    requirements: list[RequirementOut]
    model_config = {"from_attributes": True}


class CaseDetailOut(CaseOut):
    messages: list[MessageOut]


class Meta(BaseModel):
    total: int
    limit: int
    offset: int


class DataOut[T](BaseModel):
    data: T


class ListOut[T](BaseModel):
    data: list[T]
    meta: Meta


class TimelineItem(BaseModel):
    kind: Literal["event", "message"]
    id: str | None = None
    at: str
    type: str | None = None
    actor: str | None = None
    data: dict[str, Any] | None = None
    direction: str | None = None
    channel: str | None = None
    sender: str | None = None
    text: str | None = None
    attachments: list[dict[str, Any]] | None = None
    meta: dict[str, Any] | None = None


# Secrets are never echoed back; sending the mask on PATCH keeps the stored value.
MASKED = "••••"


class BusinessOut(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    inbound_email: str
    from_email: str
    escalation_email: str
    widget_public_key: str
    api_key_prefix: str
    dial_phone_number: str | None
    context_text: str
    followup_schedule_hours: list[int]
    max_followups: int
    outbound_webhook_url: str | None
    allowed_origins: list[str] = Field(default_factory=list)
    order_email_senders: list[str] = Field(default_factory=list)
    slack_webhook_url: str | None = None
    """``MASKED`` when a Slack webhook is configured (the URL itself is a secret)."""
    model_config = {"from_attributes": True}

    @model_validator(mode="before")
    @classmethod
    def _lift_settings(cls, value: Any) -> Any:
        """Fields kept in the ``Business.settings`` JSON are surfaced as plain fields."""
        if isinstance(value, dict) or not hasattr(value, "settings"):
            return value
        data = {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)}
        settings = value.settings or {}
        data["allowed_origins"] = list(settings.get("allowed_origins") or [])
        data["order_email_senders"] = list(settings.get("order_email_senders") or [])
        data["slack_webhook_url"] = MASKED if settings.get("slack_webhook_url") else None
        return data


_ORIGIN_RE = re.compile(r"^https?://[^/\s?#]+$")
# An address (orders@shopify.com) or a bare domain (amazon.com).
_SENDER_RE = re.compile(r"^(?:[^@\s]+@)?[a-z0-9-]+(?:\.[a-z0-9-]+)+$")


def normalize_origin(origin: str) -> str:
    return origin.strip().rstrip("/").lower()


class BusinessUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    escalation_email: EmailStr | None = None
    from_email: EmailStr | None = None
    context_text: str | None = Field(default=None, max_length=20000)
    followup_schedule_hours: list[int] | None = Field(default=None, min_length=1, max_length=10)
    max_followups: int | None = Field(default=None, ge=0, le=10)
    outbound_webhook_url: str | None = Field(default=None, max_length=2000)
    prefer_email_over_sms: bool | None = None
    outbound_webhook_secret: str | None = Field(default=None, max_length=128)
    slack_webhook_url: str | None = Field(default=None, max_length=2000)
    dial_number_id: str | None = None
    dial_phone_number: str | None = None
    allowed_origins: list[str] | None = Field(default=None, max_length=50)
    order_email_senders: list[str] | None = Field(default=None, max_length=50)

    @field_validator("slack_webhook_url")
    @classmethod
    def _check_slack_url(cls, value: str | None) -> str | None:
        """Empty clears it, the mask keeps it, anything else must be an https URL."""
        if value is None:
            return None
        value = value.strip()
        if value in ("", MASKED):
            return value
        if not value.startswith("https://") or len(value) <= len("https://"):
            raise ValueError("slack_webhook_url must be an https:// URL")
        return value

    @field_validator("order_email_senders")
    @classmethod
    def _check_senders(cls, value: list[str] | None) -> list[str] | None:
        """Lowercase addresses or domains, blanks and duplicates dropped."""
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            sender = raw.strip().lower()
            if not sender:
                continue
            if not _SENDER_RE.match(sender):
                raise ValueError(f"{raw!r} is not an email address or domain")
            if sender not in cleaned:
                cleaned.append(sender)
        return cleaned

    @field_validator("allowed_origins")
    @classmethod
    def _check_origins(cls, value: list[str] | None) -> list[str] | None:
        """Normalize to scheme://host[:port], drop blanks and duplicates, reject paths."""
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            origin = normalize_origin(raw)
            if not origin:
                continue
            if not _ORIGIN_RE.match(origin):
                raise ValueError(f"{raw!r} is not an origin (expected scheme://host[:port])")
            if origin not in cleaned:
                cleaned.append(origin)
        return cleaned


class CreateCaseIn(BaseModel):
    """Open a case from any source the business already has (ticket form, CRM, phone log)."""

    customer_email: EmailStr | None = None
    customer_phone: str | None = Field(default=None, max_length=32)
    customer_name: str | None = Field(default=None, max_length=200)
    subject: str | None = Field(default=None, max_length=500)
    text: str = Field(min_length=1, max_length=20000)
    channel: Literal["api", "email", "sms", "webchat"] = "api"
    external_ref: str | None = Field(default=None, max_length=200)


class HumanMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


class ResolveIn(BaseModel):
    resolution_type: str = Field(default="other", max_length=64)
    notes: str = Field(default="", max_length=5000)


class EscalateIn(BaseModel):
    reason: str = Field(min_length=1, max_length=500)
    summary_for_human: str = Field(default="", max_length=5000)


class SimulateEmailIn(BaseModel):
    to: EmailStr
    from_email: EmailStr
    from_name: str | None = None
    subject: str = "Support request"
    text: str = Field(min_length=1)
    attachments: list[dict[str, Any]] = Field(default_factory=list)
    message_id: str | None = None


class SimulateSmsIn(BaseModel):
    to: str
    from_phone: str
    body: str = Field(min_length=1)


class CreateBusinessIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")
    inbound_email: EmailStr
    from_email: EmailStr | None = None
    escalation_email: EmailStr
    context_text: str = ""
    dial_phone_number: str | None = None
    dial_number_id: str | None = None


class CreateBusinessOut(BusinessOut):
    api_key: str


class RunJobsIn(BaseModel):
    advance_hours: float = Field(default=0, ge=0, le=24 * 60)
    max_rounds: int = Field(default=20, ge=1, le=200)
    business_id: uuid.UUID | None = None  # only run this business's jobs


class RunJobsOut(BaseModel):
    ran: int
    now: datetime


class InsightSnapshotOut(BaseModel):
    id: uuid.UUID
    period_start: datetime
    period_end: datetime
    case_count: int
    model: str
    report: dict[str, Any]
    generated_at: datetime
    model_config = {"from_attributes": True}


class MagicLinkIn(BaseModel):
    email: EmailStr


class MagicLinkOut(BaseModel):
    ok: bool = True
    link: str | None = None
    """The emailed link, echoed in dev only so local testing needs no mailbox."""


class MagicLinkExchangeIn(BaseModel):
    token: str = Field(min_length=1, max_length=200)


class MemberOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    role: str
    created_at: datetime
    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    member: MemberOut
    business: BusinessOut


class InviteIn(BaseModel):
    email: EmailStr
    name: str = Field(default="", max_length=200)
