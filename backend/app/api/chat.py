"""Widget-facing chat API. Auth: the business's public widget key plus a per-session token."""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select

from app.api.deps import RateLimitWidget, ServicesDep, SessionDep
from app.attachments import public_view, resolve_stored, store_uploads
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
STREAM_POLL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 25.0


class StartSessionIn(BaseModel):
    widget_key: str | None = None
    email: EmailStr
    name: str | None = Field(default=None, max_length=200)
    message: str = Field(min_length=1, max_length=MAX_TEXT)
    page_url: str | None = Field(default=None, max_length=2000)


class ChatMessageIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


class ChatAttachmentOut(BaseModel):
    index: int
    filename: str
    content_type: str | None = None
    size: int | None = None


class ChatMessageOut(BaseModel):
    id: uuid.UUID
    sender: str
    direction: str
    text: str
    at: datetime
    attachments: list[ChatAttachmentOut] = Field(default_factory=list)


class SessionOut(BaseModel):
    token: str
    case_id: uuid.UUID
    state: str
    messages: list[ChatMessageOut]


class PollOut(BaseModel):
    state: str
    messages: list[ChatMessageOut]


def _message_out(m: Message) -> ChatMessageOut:
    return ChatMessageOut(
        id=m.id,
        sender=m.sender,
        direction=m.direction,
        text=m.body_text,
        at=m.created_at,
        attachments=[
            ChatAttachmentOut(**public_view(a, i)) for i, a in enumerate(m.attachments or [])
        ],
    )


def _visible(messages: list[Message], after: datetime | None) -> list[ChatMessageOut]:
    out = []
    for m in messages:
        if m.direction == Direction.INTERNAL:
            continue
        if after is not None and m.created_at <= after:
            continue
        out.append(_message_out(m))
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


@router.post(
    "/sessions/{token}/attachments",
    response_model=PollOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[RateLimitWidget],
)
async def post_attachments(
    token: str,
    session: SessionDep,
    services: ServicesDep,
    files: Annotated[list[UploadFile], File(description="Images or PDFs, up to 5 x 8 MB")],
    caption: Annotated[str | None, Form(max_length=MAX_TEXT)] = None,
) -> PollOut:
    """Send photos (or PDFs) with an optional caption; the agent sees them on its next turn."""
    chat, case = await _session_or_404(session, token)
    business = (
        await session.execute(select(Business).where(Business.id == chat.business_id))
    ).scalar_one()
    data_dir = services.settings.data_dir
    stored = await store_uploads(files, data_dir / "attachments" / str(case.id), data_dir)
    images = sum(1 for a in stored if str(a["content_type"]).startswith("image/"))
    if images == len(stored):
        summary = "Sent a photo" if images == 1 else f"Sent {images} photos"
    elif images == 0:
        summary = "Sent a file" if len(stored) == 1 else f"Sent {len(stored)} files"
    else:
        summary = f"Sent {len(stored)} files"
    text = caption.strip() if caption and caption.strip() else summary
    now = services.clock.now()
    chat.last_seen_at = now
    await append_inbound(
        session,
        business,
        case,
        text=text,
        now=now,
        channel=Channel.WEBCHAT,
        attachments=stored,
        meta={"source": "widget", "summary": summary},
    )
    await session.commit()
    await session.refresh(case)
    return PollOut(state=case.state, messages=_visible(case.messages, None))


@router.get("/sessions/{token}/attachments/{message_id}/{index}", response_class=FileResponse)
async def get_attachment(
    token: str, message_id: uuid.UUID, index: int, session: SessionDep, services: ServicesDep
) -> FileResponse:
    """Stream one attachment of a message in this chat (for the widget's own thumbnails)."""
    _, case = await _session_or_404(session, token)
    message = next(
        (m for m in case.messages if m.id == message_id and m.direction != Direction.INTERNAL),
        None,
    )
    attachments = (message.attachments or []) if message is not None else []
    if message is None or not 0 <= index < len(attachments):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment not found")
    att = attachments[index]
    path = resolve_stored(services.settings.data_dir, att)
    if path is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "attachment file is not available")
    return FileResponse(
        path,
        media_type=str(att.get("content_type") or "application/octet-stream"),
        filename=str(att.get("filename") or path.name),
        content_disposition_type="inline",
        headers={"Cache-Control": "private, max-age=3600"},
    )


def _sse(event: str, data: Any) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n".encode()


@router.get("/sessions/{token}/stream")
async def stream_session(
    token: str,
    request: Request,
    session: SessionDep,
    services: ServicesDep,
    after: Annotated[datetime | None, Query()] = None,
) -> StreamingResponse:
    """Server-sent events: `message` for each new visible message, `state` when the case moves.

    The first event is always the current state. Polls the database every second; a comment
    line is sent every 25 s so proxies keep the connection open.
    """
    chat, case = await _session_or_404(session, token)
    chat.last_seen_at = services.clock.now()
    await session.commit()
    # The request's session is released now; each poll below opens a short-lived one so a
    # long-lived stream does not pin a database connection.
    case_id, initial_state = case.id, case.state
    initial = _visible(case.messages, after)

    async def events() -> AsyncIterator[bytes]:
        state = initial_state
        seen: set[uuid.UUID] = set()
        yield _sse("state", {"state": state})
        for m in initial:
            seen.add(m.id)
            yield _sse("message", m.model_dump(mode="json"))
        idle = 0.0
        while not await request.is_disconnected():
            await asyncio.sleep(STREAM_POLL_SECONDS)
            idle += STREAM_POLL_SECONDS
            async with services.session_factory() as db:
                chat_row = (
                    await db.execute(select(ChatSession).where(ChatSession.token == token))
                ).scalar_one_or_none()
                if chat_row is None or not chat_row.active:
                    yield _sse("state", {"state": "closed"})
                    return
                current = await load_case(db, case_id)
                if current is None:
                    return
                fresh = [m for m in _visible(current.messages, after) if m.id not in seen]
                new_state = current.state
            if new_state != state:
                state = new_state
                idle = 0.0
                yield _sse("state", {"state": state})
            for m in fresh:
                seen.add(m.id)
                idle = 0.0
                yield _sse("message", m.model_dump(mode="json"))
            if idle >= STREAM_HEARTBEAT_SECONDS:
                idle = 0.0
                yield b": keep-alive\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
