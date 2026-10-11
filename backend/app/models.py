from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class CaseState(StrEnum):
    NEW = "new"
    GATHERING = "gathering"
    AWAITING_CUSTOMER = "awaiting_customer"
    RESOLVED = "resolved"
    ESCALATED = "escalated"
    ABANDONED = "abandoned"


TERMINAL_STATES = frozenset({CaseState.RESOLVED, CaseState.ESCALATED, CaseState.ABANDONED})


class Channel(StrEnum):
    EMAIL = "email"
    SMS = "sms"
    VOICE = "voice"
    WEBCHAT = "webchat"
    API = "api"


class Category(StrEnum):
    REFUND = "refund"
    RETURN = "return"
    DAMAGED = "damaged"
    MISSING = "missing"
    BILLING = "billing"
    QUESTION = "question"
    OTHER = "other"


class Direction(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"
    INTERNAL = "internal"


class Sender(StrEnum):
    CUSTOMER = "customer"
    AGENT = "agent"
    HUMAN = "human"
    SYSTEM = "system"


class RequirementStatus(StrEnum):
    MISSING = "missing"
    PROVIDED = "provided"
    NOT_APPLICABLE = "not_applicable"


class JobKind(StrEnum):
    AGENT_TURN = "agent_turn"
    FOLLOWUP = "followup"
    FETCH_CALL_TRANSCRIPT = "fetch_call_transcript"
    INSIGHTS = "insights"
    NOTIFY_ESCALATION = "notify_escalation"
    DELIVER_WEBHOOK = "deliver_webhook"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


def _uuid_col() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class Business(TimestampMixin, Base):
    __tablename__ = "businesses"

    id: Mapped[uuid.UUID] = _uuid_col()
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)
    inbound_email: Mapped[str] = mapped_column(String(320), unique=True)
    from_email: Mapped[str] = mapped_column(String(320))
    escalation_email: Mapped[str] = mapped_column(String(320))
    api_key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    api_key_prefix: Mapped[str] = mapped_column(String(16))
    widget_public_key: Mapped[str] = mapped_column(String(64), unique=True)
    dial_number_id: Mapped[str | None] = mapped_column(String(100))
    dial_phone_number: Mapped[str | None] = mapped_column(String(32))
    context_text: Mapped[str] = mapped_column(Text, default="")
    followup_schedule_hours: Mapped[list[int]] = mapped_column(JSONB, default=lambda: [24, 72, 168])
    max_followups: Mapped[int] = mapped_column(Integer, default=3)
    outbound_webhook_url: Mapped[str | None] = mapped_column(String(2000))
    outbound_webhook_secret: Mapped[str | None] = mapped_column(String(128))
    settings: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class Customer(TimestampMixin, Base):
    __tablename__ = "customers"
    __table_args__ = (
        UniqueConstraint("business_id", "email", name="uq_customer_business_email"),
        UniqueConstraint("business_id", "phone", name="uq_customer_business_phone"),
    )

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(32))
    name: Mapped[str | None] = mapped_column(String(200))


class Case(TimestampMixin, Base):
    __tablename__ = "cases"
    __table_args__ = (Index("ix_cases_business_state", "business_id", "state"),)

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(String(32), default=CaseState.NEW)
    channel: Mapped[str] = mapped_column(String(16))
    category: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(String(500))
    summary: Mapped[str | None] = mapped_column(Text)
    sentiment: Mapped[str | None] = mapped_column(String(16))
    urgency: Mapped[str | None] = mapped_column(String(16))
    remedy_requested: Mapped[str | None] = mapped_column(String(500))
    external_ref: Mapped[str | None] = mapped_column(String(200))
    email_thread_message_ids: Mapped[list[str]] = mapped_column(JSONB, default=list)
    followups_sent: Mapped[int] = mapped_column(Integer, default=0)
    last_customer_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_agent_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    escalation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    customer: Mapped[Customer] = relationship(lazy="selectin")
    messages: Mapped[list[Message]] = relationship(
        back_populates="case", order_by="Message.created_at", lazy="selectin"
    )
    requirements: Mapped[list[Requirement]] = relationship(
        back_populates="case", order_by="Requirement.created_at", lazy="selectin"
    )


class Message(TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (Index("ix_messages_case_created", "case_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_col()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    direction: Mapped[str] = mapped_column(String(16))
    channel: Mapped[str] = mapped_column(String(16))
    sender: Mapped[str] = mapped_column(String(16))
    body_text: Mapped[str] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    provider_thread_ids: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)

    case: Mapped[Case] = relationship(back_populates="messages")


class Requirement(TimestampMixin, Base):
    __tablename__ = "requirements"
    __table_args__ = (UniqueConstraint("case_id", "key", name="uq_requirement_case_key"),)

    id: Mapped[uuid.UUID] = _uuid_col()
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    key: Mapped[str] = mapped_column(String(64))
    label: Mapped[str] = mapped_column(String(200))
    why_needed: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=RequirementStatus.MISSING)
    value: Mapped[Any | None] = mapped_column(JSONB)
    asked_count: Mapped[int] = mapped_column(Integer, default=0)
    provided_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    case: Mapped[Case] = relationship(back_populates="requirements")


class Job(TimestampMixin, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        Index("ix_jobs_due", "status", "due_at"),
        Index(
            "uq_jobs_single_pending_agent_turn",
            "case_id",
            unique=True,
            postgresql_where=text("kind = 'agent_turn' AND status = 'pending'"),
        ),
        # Recurring jobs (insights, digest) carry no case: one pending run per business and kind.
        Index(
            "uq_jobs_single_pending_recurring",
            "business_id",
            "kind",
            unique=True,
            postgresql_where=text(
                "status = 'pending' AND case_id IS NULL AND kind IN ('insights', 'digest')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PENDING)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Event(TimestampMixin, Base):
    __tablename__ = "events"
    __table_args__ = (Index("ix_events_case_created", "case_id", "created_at"),)

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(String(64))
    actor: Mapped[str] = mapped_column(String(16))
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class ChatSession(TimestampMixin, Base):
    __tablename__ = "chat_sessions"

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("cases.id", ondelete="CASCADE"))
    customer_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("customers.id", ondelete="CASCADE"))
    token: Mapped[str] = mapped_column(String(64), unique=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class InsightSnapshot(TimestampMixin, Base):
    __tablename__ = "insight_snapshots"

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    period_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    case_count: Mapped[int] = mapped_column(Integer)
    model: Mapped[str] = mapped_column(String(64))
    report: Mapped[dict[str, Any]] = mapped_column(JSONB)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Order(TimestampMixin, Base):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("business_id", "source", "source_order_id", name="uq_order_source_id"),
        Index("ix_orders_business_number", "business_id", "order_number"),
        Index("ix_orders_business_email", "business_id", "customer_email"),
        Index("ix_orders_business_phone", "business_id", "customer_phone"),
    )

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    source: Mapped[str] = mapped_column(String(32))
    source_order_id: Mapped[str] = mapped_column(String(200))
    order_number: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="unknown")
    customer_email: Mapped[str | None] = mapped_column(String(320))
    customer_phone: Mapped[str | None] = mapped_column(String(32))
    customer_name: Mapped[str | None] = mapped_column(String(200))
    items: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    total: Mapped[Any | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(String(8))
    ordered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tracking: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    shipping_address: Mapped[str | None] = mapped_column(Text)
    notes: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class MemberRole(StrEnum):
    OWNER = "owner"
    MEMBER = "member"


class TeamMember(TimestampMixin, Base):
    __tablename__ = "team_members"
    __table_args__ = (
        UniqueConstraint("business_id", "email", name="uq_team_member_business_email"),
    )

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(320))
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(16), default=MemberRole.MEMBER)

    business: Mapped[Business] = relationship(lazy="selectin")


class LoginToken(TimestampMixin, Base):
    __tablename__ = "login_tokens"

    id: Mapped[uuid.UUID] = _uuid_col()
    team_member_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("team_members.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DashboardSession(TimestampMixin, Base):
    __tablename__ = "dashboard_sessions"

    id: Mapped[uuid.UUID] = _uuid_col()
    team_member_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("team_members.id", ondelete="CASCADE")
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    member: Mapped[TeamMember] = relationship(lazy="selectin")


class Lesson(TimestampMixin, Base):
    """Something the business taught the agent: a human override, reply, or decision on a case."""

    __tablename__ = "lessons"
    __table_args__ = (Index("ix_lessons_business_active", "business_id", "active"),)

    id: Mapped[uuid.UUID] = _uuid_col()
    business_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("businesses.id", ondelete="CASCADE"))
    case_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("cases.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(32))
    category: Mapped[str | None] = mapped_column(String(32))
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
