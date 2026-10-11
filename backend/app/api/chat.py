"""Widget-facing chat API. Auth: the business's public widget key plus a per-session token."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import RateLimitWidget, ServicesDep, SessionDep
from app.auth import business_for_widget_key
from app.cases.service import (
    append_inbound,
    find_case_for_customer,
    get_or_create_customer,
    load_case,
    open_case,
)
from app.models import Business, Case, Channel, ChatSession, Direction, Message
from app.schemas import normalize_origin

router = APIRouter(prefix="/v1/chat", tags=["chat"])

MAX_TEXT = 4000


class StartSessionIn(BaseModel):
    widget_key: str | None = None
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    page_url: str | None = Field(default=None, max_length=2000)


class ChatMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    sender: str
    direction: str
    text: str
    at: datetime


class SessionOut(BaseModel):
    token: str
    case_id: uuid.UUID
    state: str
    messages: list[ChatMessageOut]


class PollOut(BaseModel):
    state: str
    messages: list[ChatMessageOut]


def _visible(messages: list[Message], after: datetime | None) -> list[ChatMessageOut]:
    out = []
    for m in messages:
        if m.direction == Direction.INTERNAL:
            continue
        if after is not None and m.created_at <= after:
            continue
        out.append(
            ChatMessageOut(
                id=m.id, sender=m.sender, direction=m.direction, text=m.body_text, at=m.created_at
            )
        )
    return out


async def _business_from_key(session: SessionDep, key: str | None) -> Business:
    if not key:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing widget key")
    business = await business_for_widget_key(session, key)
    if business is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid widget key")
    return business


def _check_origin(business: Business, origin: str | None) -> None:
    """Browsers always send Origin from the widget; a non-empty allow-list must contain it."""
    allowed = (business.settings or {}).get("allowed_origins") or []
    if origin and allowed and normalize_origin(origin) not in allowed:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "origin not allowed for this widget")


async def _session_or_404(session: SessionDep, token: str) -> tuple[ChatSession, Case]:
    chat = (
        await session.execute(select(ChatSession).where(ChatSession.token == token))
    ).scalar_one_or_none()
    if chat is None or not chat.active:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "chat session not found")
    case = await load_case(session, chat.case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "case not found")
    return chat, case


@router.post(
    "/sessions",
    response_model=SessionOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RateLimitWidget],
)
async def start_session(
    payload: StartSessionIn,
    session: SessionDep,
    services: ServicesDep,
    response: Response,
    x_widget_key: Annotated[str | None, Header()] = None,
    origin: Annotated[str | None, Header()] = None,
) -> SessionOut:
    business = await _business_from_key(session, payload.widget_key or x_widget_key)
    _check_origin(business, origin)
    now = services.clock.now()
    customer = await get_or_create_customer(
        session, business, email=str(payload.email), name=payload.name
    )
    case, needs_reopen = await find_case_for_customer(
        session,
        business,
        customer,
        now=now,
        reopen_window_days=services.settings.reopen_window_days,
    )
    if case is None or needs_reopen or case.channel != Channel.WEBCHAT:
        # A fresh chat gets its own webchat case even if an email case exists; the agent sees both.
        case = await open_case(
            session, business, customer, channel=Channel.WEBCHAT, now=now, subject="Website chat"
        )
    chat = ChatSession(
        business_id=business.id,
        case_id=case.id,
        customer_id=customer.id,
        token=secrets.token_urlsafe(24),
        last_seen_at=now,
    )
    session.add(chat)
    await append_inbound(
        session,
        business,
        case,
        text=payload.message,
        now=now,
        channel=Channel.WEBCHAT,
        meta={"source": "widget", "page_url": payload.page_url},
    )
    await session.commit()
    await session.refresh(case)
    response.headers["Location"] = f"/v1/chat/sessions/{chat.token}"
    return SessionOut(
        token=chat.token, case_id=case.id, state=case.state, messages=_visible(case.messages, None)
    )


@router.post(
    "/sessions/{token}/messages", response_model=PollOut, status_code=status.HTTP_201_CREATED
)
async def post_message(
    token: str, payload: ChatMessageIn, session: SessionDep, services: ServicesDep
) -> PollOut:
    chat, case = await _session_or_404(session, token)
    business = (
        await session.execute(select(Business).where(Business.id == chat.business_id))
    ).scalar_one()
    now = services.clock.now()
    chat.last_seen_at = now
    await append_inbound(
        session,
        business,
        case,
        text=payload.text,
        now=now,
        channel=Channel.WEBCHAT,
        meta={"source": "widget"},
    )
    await session.commit()
    await session.refresh(case)
    return PollOut(state=case.state, messages=_visible(case.messages, None))


@router.get("/sessions/{token}/messages", response_model=PollOut)
async def poll_messages(
    token: str,
    session: SessionDep,
    services: ServicesDep,
    after: Annotated[datetime | None, Query()] = None,
) -> PollOut:
    chat, case = await _session_or_404(session, token)
    chat.last_seen_at = services.clock.now()
    await session.commit()
    return PollOut(state=case.state, messages=_visible(case.messages, after))
