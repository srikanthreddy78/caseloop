"""Tools the agent can call. Provider-agnostic: specs are JSON schema, dispatch is by name."""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.requirements import canonical_key
from app.cases import state as sm
from app.cases.service import cancel_pending_jobs, enqueue_job, record_event, transition_case
from app.channels.base import ChannelUnavailable, SendResult
from app.channels.fallback import delivery_channel
from app.channels.registry import ChannelRegistry
from app.clock import Clock
from app.models import (
    Business,
    Case,
    CaseState,
    Channel,
    Customer,
    Direction,
    JobKind,
    Message,
    Requirement,
    RequirementStatus,
    Sender,
)

log = logging.getLogger(__name__)

MAX_TOOL_CALLS = 12

_PII_PATTERNS = [
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[card number removed]"),
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[id number removed]"),
]


def scrub_pii(text: str) -> str:
    for pattern, replacement in _PII_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


class ToolError(Exception):
    pass


@dataclass
class TurnContext:
    session: AsyncSession
    business: Business
    case: Case
    customer: Customer
    channels: ChannelRegistry
    clock: Clock
    trigger: str
    orders: Any = None  # app.orders.service.OrderService | None
    orders_found: list[dict[str, Any]] = field(default_factory=list)
    messages_sent: int = 0
    messages_failed: int = 0  # of messages_sent, how many the channel could not deliver
    terminal_tool: str | None = None
    tool_calls: int = 0
    log: list[dict[str, Any]] = field(default_factory=list)


TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "get_case",
        "description": "Return the current case: state, category, requirements, and the recent conversation.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
    {
        "name": "update_requirements",
        "description": "Mark requirements as provided (with the value the customer gave) or not_applicable.",
        "parameters": {
            "type": "object",
            "properties": {
                "updates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "key": {"type": "string"},
                            "status": {
                                "type": "string",
                                "enum": ["provided", "not_applicable", "missing"],
                            },
                            "value": {"type": ["string", "null"]},
                        },
                        "required": ["key", "status", "value"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["updates"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_requirement",
        "description": "Add something the business needs from the customer that the case does not list yet.",
        "parameters": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "label": {"type": "string"},
                "why_needed": {"type": "string"},
            },
            "required": ["key", "label", "why_needed"],
            "additionalProperties": False,
        },
    },
    {
        "name": "send_message",
        "description": "Send one message to the customer on the case's channel. Only one per turn.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    {
        "name": "schedule_followup",
        "description": "After asking the customer for something, wait this many hours before nudging them. Ends the turn.",
        "parameters": {
            "type": "object",
            "properties": {"hours": {"type": "integer"}, "reason": {"type": "string"}},
            "required": ["hours", "reason"],
            "additionalProperties": False,
        },
    },
    {
        "name": "close_case",
        "description": "Resolve the case. Requires every requirement to be provided or not_applicable unless resolution_type is no_action_needed. Ends the turn.",
        "parameters": {
            "type": "object",
            "properties": {
                "resolution_type": {
                    "type": "string",
                    "enum": [
                        "refund_issued",
                        "replacement_sent",
                        "return_approved",
                        "credit_issued",
                        "information_provided",
                        "no_action_needed",
                        "other",
                    ],
                },
                "notes": {"type": "string"},
            },
            "required": ["resolution_type", "notes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "escalate",
        "description": "Hand the case to a human with a complete summary of the situation and what they need to decide. Ends the turn.",
        "parameters": {
            "type": "object",
            "properties": {"reason": {"type": "string"}, "summary_for_human": {"type": "string"}},
            "required": ["reason", "summary_for_human"],
            "additionalProperties": False,
        },
    },
    {
        "name": "request_phone_call",
        "description": "Place an AI voice call to the customer with these instructions. Only works when the business has a phone line and the customer has a phone number.",
        "parameters": {
            "type": "object",
            "properties": {"instruction": {"type": "string"}},
            "required": ["instruction"],
            "additionalProperties": False,
        },
    },
    {
        "name": "lookup_order",
        "description": "Look the order up in every system the business has connected (Shopify, Amazon, their own site, imported files), always under this customer's own email and phone. Results say which source they came from; an order marked found_but_unverified belongs to different contact details, so ask the customer to confirm before using it. Use it before deciding anything that depends on delivery date, order status, tracking, or amount.",
        "parameters": {
            "type": "object",
            "properties": {"order_number": {"type": ["string", "null"]}},
            "required": ["order_number"],
            "additionalProperties": False,
        },
    },
    {
        "name": "add_internal_note",
        "description": "Leave a note for the business team on the case timeline. The customer never sees it.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
            "additionalProperties": False,
        },
    },
]


def case_snapshot(ctx: TurnContext, *, recent: int = 20) -> dict[str, Any]:
    case = ctx.case
    msgs = case.messages[-recent:]
    return {
        "case_id": str(case.id),
        "state": case.state,
        "channel": case.channel,
        "category": case.category,
        "subject": case.subject,
        "summary": case.summary,
        "sentiment": case.sentiment,
        "urgency": case.urgency,
        "remedy_requested": case.remedy_requested,
        "followups_sent": case.followups_sent,
        "max_followups": ctx.business.max_followups,
        "customer": {
            "name": ctx.customer.name,
            "email": ctx.customer.email,
            "phone": ctx.customer.phone,
        },
        "orders": ctx.orders_found,
        "requirements": [
            {
                "key": r.key,
                "label": r.label,
                "status": r.status,
                "value": r.value,
                "asked_count": r.asked_count,
            }
            for r in case.requirements
        ],
        "conversation": [
            {
                "at": m.created_at.isoformat() if m.created_at else None,
                "from": m.sender,
                "direction": m.direction,
                "channel": m.channel,
                "text": scrub_pii(m.body_text) if m.direction == Direction.INBOUND else m.body_text,
                "attachments": [a.get("filename") for a in (m.attachments or [])],
            }
            for m in msgs
        ],
    }


class CaseTools:
    """Executes tool calls for one turn. Guardrails live here, not only in the prompt."""

    def __init__(self, ctx: TurnContext) -> None:
        self.ctx = ctx

    def specs(self) -> list[dict[str, Any]]:
        return TOOL_SPECS

    async def dispatch(self, name: str, args: dict[str, Any]) -> str:
        ctx = self.ctx
        ctx.tool_calls += 1
        if ctx.tool_calls > MAX_TOOL_CALLS:
            raise ToolError("tool call limit reached for this turn")
        if ctx.terminal_tool is not None and name != "get_case":
            return _err(f"turn already ended by {ctx.terminal_tool}; no further actions allowed")
        handler = getattr(self, f"tool_{name}", None)
        if handler is None:
            return _err(f"unknown tool {name}")
        try:
            result = await handler(**args)
        except TypeError as exc:
            result = _err(f"bad arguments: {exc}")
        except ToolError as exc:
            result = _err(str(exc))
        await record_event(
            ctx.session,
            business_id=ctx.business.id,
            case_id=ctx.case.id,
            type="tool_called",
            actor=Sender.AGENT,
            data={"tool": name, "args": _trim(args), "result": result[:500]},
        )
        ctx.log.append({"tool": name, "args": args, "result": result})
        return result

    # ---- read ------------------------------------------------------------------------------

    async def tool_get_case(self) -> str:
        return json.dumps(case_snapshot(self.ctx), default=str)

    # ---- requirements ----------------------------------------------------------------------

    async def tool_update_requirements(self, updates: list[dict[str, Any]]) -> str:
        case = self.ctx.case
        by_key = {r.key: r for r in case.requirements}
        changed: list[str] = []
        for upd in updates:
            key = canonical_key(str(upd.get("key", "")))
            status = str(upd.get("status", "")).strip()
            value = upd.get("value")
            req = by_key.get(key)
            if req is None:
                continue
            if status not in {s.value for s in RequirementStatus}:
                continue
            if req.status == RequirementStatus.PROVIDED and status == RequirementStatus.MISSING:
                continue  # never downgrade
            req.status = status
            if value is not None:
                req.value = value
            if status == RequirementStatus.PROVIDED and self.ctx.case.messages:
                req.provided_message_id = self.ctx.case.messages[-1].id
            changed.append(key)
        await self.ctx.session.flush()
        return json.dumps({"updated": changed, "missing": _missing_keys(case)})

    async def tool_add_requirement(self, key: str, label: str, why_needed: str) -> str:
        key = canonical_key(key)
        if not key:
            raise ToolError("key is required")
        case = self.ctx.case
        if any(r.key == key for r in case.requirements):
            return json.dumps({"ok": True, "note": "already exists"})
        req = Requirement(case_id=case.id, key=key, label=label[:200], why_needed=why_needed)
        self.ctx.session.add(req)
        await self.ctx.session.flush()
        case.requirements.append(req)
        return json.dumps({"ok": True, "missing": _missing_keys(case)})

    # ---- outbound ------------------------------------------------------------------------

    async def tool_send_message(self, text: str) -> str:
        ctx = self.ctx
        if ctx.messages_sent >= 1:
            raise ToolError("only one message per turn; it was already sent")
        text = text.strip()
        if not text:
            raise ToolError("message text is empty")
        channel = delivery_channel(ctx.case, ctx.customer, ctx.business)
        if channel == Channel.SMS and len(text) > 320:
            raise ToolError("SMS messages must be under 320 characters")
        delivery_error: str | None = None
        result = SendResult()
        try:
            adapter = ctx.channels.get(channel)
            result = await adapter.send(
                business=ctx.business, case=ctx.case, customer=ctx.customer, text=text
            )
        except ChannelUnavailable as exc:
            delivery_error = str(exc)
        except Exception as exc:
            log.exception("delivery failed on case %s via %s", ctx.case.id, ctx.case.channel)
            delivery_error = f"{type(exc).__name__}: {str(exc)[:300]}"
        now = ctx.clock.now()
        msg = Message(
            case_id=ctx.case.id,
            direction=Direction.OUTBOUND,
            channel=channel,
            sender=Sender.AGENT,
            body_text=text,
            provider_message_id=result.provider_message_id,
            provider_thread_ids=result.thread_ids or {},
            meta={"delivery": "failed", "error": delivery_error}
            if delivery_error
            else {"delivery": "sent"},
        )
        ctx.session.add(msg)
        rfc_id = (result.thread_ids or {}).get("rfc_message_id")
        if channel == Channel.EMAIL and rfc_id:
            ids = list(ctx.case.email_thread_message_ids or [])
            ids.append(rfc_id)
            ctx.case.email_thread_message_ids = ids
        ctx.messages_sent += 1
        if delivery_error is None:
            # Only a delivered message counts as having asked the customer for anything.
            ctx.case.last_agent_message_at = now
            for r in ctx.case.requirements:
                if r.status == RequirementStatus.MISSING:
                    r.asked_count += 1
        else:
            ctx.messages_failed += 1
        await ctx.session.flush()
        ctx.case.messages.append(msg)
        await record_event(
            ctx.session,
            business_id=ctx.business.id,
            case_id=ctx.case.id,
            type="delivery_failed" if delivery_error else "message_sent",
            actor=Sender.AGENT,
            data={"message_id": str(msg.id), "channel": ctx.case.channel, "error": delivery_error},
        )
        if delivery_error:
            return json.dumps(
                {
                    "ok": False,
                    "delivered": False,
                    "message_id": str(msg.id),
                    "error": delivery_error,
                    "instruction": "Do not schedule a follow-up. Call escalate with reason "
                    "'delivery_failed' so a person can resend.",
                }
            )
        return json.dumps({"ok": True, "delivered": True, "message_id": str(msg.id)})

    async def tool_request_phone_call(self, instruction: str) -> str:
        ctx = self.ctx
        if ctx.channels.voice is None or not ctx.business.dial_number_id:
            raise ToolError("phone calls are not configured for this business")
        if not ctx.customer.phone:
            raise ToolError("customer has no phone number on file")
        call_id = await ctx.channels.voice.start_call(
            business=ctx.business, case=ctx.case, customer=ctx.customer, instruction=instruction
        )
        await record_event(
            ctx.session,
            business_id=ctx.business.id,
            case_id=ctx.case.id,
            type="call_started",
            actor=Sender.AGENT,
            data={"call_id": call_id, "instruction": instruction[:500]},
        )
        return json.dumps({"ok": True, "call_id": call_id})

    async def tool_lookup_order(
        self, order_number: str | None = None, email: str | None = None
    ) -> str:
        from app.orders.base import OrderQuery

        # The model never chooses whose orders to look at: the case customer's own email and
        # phone are the only identifiers used, whatever `email` it passed.
        del email
        ctx = self.ctx
        if ctx.orders is None:
            return json.dumps(
                {"found": [], "sources_checked": [], "no_order_system_connected": True}
            )
        query = OrderQuery(
            order_number=order_number or None, email=ctx.customer.email, phone=ctx.customer.phone
        )
        result = await ctx.orders.lookup(ctx.session, ctx.business, query, now=ctx.clock.now())
        summary = result.customer_summary(ctx.customer.email, ctx.customer.phone)
        ctx.orders_found = summary["found"]
        await record_event(
            ctx.session,
            business_id=ctx.business.id,
            case_id=ctx.case.id,
            type="order_lookup",
            actor=Sender.AGENT,
            data={
                "query": query.model_dump(exclude_none=True),
                "found": len(result.orders),
                "sources": summary["sources_checked"],
                "errors": result.errors,
            },
        )
        return json.dumps(summary, default=str)

    async def tool_add_internal_note(self, text: str) -> str:
        ctx = self.ctx
        msg = Message(
            case_id=ctx.case.id,
            direction=Direction.INTERNAL,
            channel=ctx.case.channel,
            sender=Sender.AGENT,
            body_text=text.strip(),
        )
        ctx.session.add(msg)
        await ctx.session.flush()
        ctx.case.messages.append(msg)
        return json.dumps({"ok": True})

    # ---- terminal ------------------------------------------------------------------------

    async def tool_schedule_followup(self, hours: int, reason: str) -> str:
        ctx = self.ctx
        case, biz = ctx.case, ctx.business
        if ctx.messages_sent == 0:
            raise ToolError("send the customer a message before scheduling a follow-up")
        if ctx.messages_failed == ctx.messages_sent:
            raise ToolError(
                "the message was not delivered, so there is nothing to follow up on; "
                "call escalate with reason 'delivery_failed' so a person can resend it"
            )
        hours = sm.clamp_followup_hours(hours, biz.followup_schedule_hours, case.followups_sent)
        if ctx.trigger == "followup_due":
            case.followups_sent += 1
        if case.state != CaseState.AWAITING_CUSTOMER:
            await transition_case(
                ctx.session,
                case,
                CaseState.AWAITING_CUSTOMER,
                actor=Sender.AGENT,
                now=ctx.clock.now(),
            )
        await cancel_pending_jobs(ctx.session, case.id, kinds=(JobKind.FOLLOWUP,))
        due = ctx.clock.now() + _hours(hours)
        await enqueue_job(
            ctx.session,
            business_id=biz.id,
            kind=JobKind.FOLLOWUP,
            case_id=case.id,
            due_at=due,
            payload={"reason": reason},
        )
        await record_event(
            ctx.session,
            business_id=biz.id,
            case_id=case.id,
            type="followup_scheduled",
            actor=Sender.AGENT,
            data={"hours": hours, "due_at": due.isoformat(), "reason": reason},
        )
        ctx.terminal_tool = "schedule_followup"
        return json.dumps({"ok": True, "hours": hours, "due_at": due.isoformat()})

    async def tool_close_case(self, resolution_type: str, notes: str) -> str:
        ctx = self.ctx
        case = ctx.case
        missing = _missing_keys(case)
        if missing and resolution_type != "no_action_needed":
            raise ToolError(f"cannot close: requirements still missing: {', '.join(missing)}")
        case.resolution = {"type": resolution_type, "notes": notes}
        await cancel_pending_jobs(ctx.session, case.id)
        await transition_case(
            ctx.session,
            case,
            CaseState.RESOLVED,
            actor=Sender.AGENT,
            now=ctx.clock.now(),
            data={"resolution_type": resolution_type},
        )
        ctx.terminal_tool = "close_case"
        return json.dumps({"ok": True, "state": case.state})

    async def tool_escalate(self, reason: str, summary_for_human: str) -> str:
        ctx = self.ctx
        case = ctx.case
        case.escalation = {"reason": reason, "summary": summary_for_human, "trigger": ctx.trigger}
        await cancel_pending_jobs(ctx.session, case.id)
        await transition_case(
            ctx.session,
            case,
            CaseState.ESCALATED,
            actor=Sender.AGENT,
            now=ctx.clock.now(),
            data={"reason": reason},
        )
        await enqueue_job(
            ctx.session,
            business_id=ctx.business.id,
            kind=JobKind.NOTIFY_ESCALATION,
            case_id=case.id,
            due_at=ctx.clock.now(),
            payload={"reason": reason},
        )
        ctx.terminal_tool = "escalate"
        return json.dumps({"ok": True, "state": case.state})


def _missing_keys(case: Case) -> list[str]:
    return [r.key for r in case.requirements if r.status == RequirementStatus.MISSING]


def _err(message: str) -> str:
    return json.dumps({"error": message})


def _trim(args: dict[str, Any]) -> dict[str, Any]:
    return {k: (v[:500] if isinstance(v, str) else v) for k, v in args.items()}


def _hours(h: int) -> Any:
    from datetime import timedelta

    return timedelta(hours=h)


def is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True
