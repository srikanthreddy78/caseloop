"""Dashboard sign-in (emailed magic links, cookie sessions) and team management."""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, HTTPException, Request, Response, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import BusinessDep, MemberDep, ServicesDep, SessionDep
from app.auth import (
    LOGIN_TOKEN_TTL,
    SESSION_COOKIE,
    SESSION_TTL,
    generate_login_token,
    generate_session_token,
    hash_token,
)
from app.container import Services
from app.models import Business, DashboardSession, LoginToken, MemberRole, TeamMember
from app.schemas import (
    BusinessOut,
    DataOut,
    InviteIn,
    MagicLinkExchangeIn,
    MagicLinkIn,
    MagicLinkOut,
    MemberOut,
    SessionOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/v1", tags=["auth"])


def _issue_login_token(session: AsyncSession, services: Services, member: TeamMember) -> str:
    """Add a single-use login token for the member and return the link to email."""
    token = generate_login_token()
    session.add(
        LoginToken(
            team_member_id=member.id,
            token_hash=hash_token(token),
            expires_at=services.clock.now() + LOGIN_TOKEN_TTL,
        )
    )
    return f"{services.settings.base_url.rstrip('/')}/dashboard/login?token={token}"


def _login_email(
    member: TeamMember, business: Business, link: str, *, invited_by: str | None = None
) -> tuple[str, str]:
    greeting = f"Hi {member.name}," if member.name else "Hi,"
    if invited_by:
        subject = f"{invited_by} added you to {business.name} on Caseloop"
        intro = (
            f"{invited_by} added you to the {business.name} team on Caseloop. "
            "Use the link below to open the dashboard:"
        )
    else:
        subject = f"Sign in to Caseloop ({business.name})"
        intro = "Here is your sign-in link for the Caseloop dashboard:"
    text = (
        f"{greeting}\n\n{intro}\n\n{link}\n\n"
        "The link works once and expires in 15 minutes. "
        "If you did not ask for it, you can ignore this email.\n"
    )
    return subject, text


async def _send(services: Services, business: Business, to: str, subject: str, text: str) -> None:
    notifier = services.channels.notifier
    if notifier is None:
        log.warning("no notifier configured; sign-in email to %s not sent", to)
        return
    try:
        await notifier.send_email(to=to, subject=subject, text=text, from_email=business.from_email)
    except Exception as exc:  # the link is still logged (and returned in dev); never 500 on email
        log.warning("sign-in email to %s could not be sent: %s", to, exc)


def _set_session_cookie(response: Response, token: str, services: Services) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=int(SESSION_TTL.total_seconds()),
        httponly=True,
        samesite="lax",
        secure=services.settings.base_url.startswith("https://"),
        path="/",
    )


def _session_out(member: TeamMember, business: Business) -> SessionOut:
    return SessionOut(
        member=MemberOut.model_validate(member), business=BusinessOut.model_validate(business)
    )


@router.post(
    "/auth/magic-link",
    response_model=DataOut[MagicLinkOut],
    status_code=status.HTTP_202_ACCEPTED,
)
async def request_magic_link(
    payload: MagicLinkIn, session: SessionDep, services: ServicesDep
) -> DataOut[MagicLinkOut]:
    """Email a sign-in link if the address belongs to a team. Always 202: never reveals membership."""
    email = str(payload.email).lower()
    members = (
        (
            await session.execute(
                select(TeamMember).where(TeamMember.email == email).order_by(TeamMember.created_at)
            )
        )
        .scalars()
        .all()
    )
    outgoing: list[tuple[Business, str, str, str]] = []
    first_link: str | None = None
    for member in members:
        link = _issue_login_token(session, services, member)
        subject, text = _login_email(member, member.business, link)
        outgoing.append((member.business, subject, text, link))
        first_link = first_link or link
    await session.commit()
    for business, subject, text, link in outgoing:
        await _send(services, business, email, subject, text)
        if services.settings.is_dev:
            log.info("magic link for %s: %s", email, link)
    return DataOut(data=MagicLinkOut(link=first_link if services.settings.is_dev else None))


@router.post("/auth/magic-link/exchange", response_model=DataOut[SessionOut])
async def exchange_magic_link(
    payload: MagicLinkExchangeIn, response: Response, session: SessionDep, services: ServicesDep
) -> DataOut[SessionOut]:
    """Turn a single-use link token into a 30-day session cookie."""
    now = services.clock.now()
    login = (
        await session.execute(
            select(LoginToken).where(LoginToken.token_hash == hash_token(payload.token))
        )
    ).scalar_one_or_none()
    if login is None or login.used_at is not None or login.expires_at <= now:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "this sign-in link is invalid or has expired"
        )
    member = await session.get(TeamMember, login.team_member_id)
    if member is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "this sign-in link is no longer valid")
    login.used_at = now
    session_token = generate_session_token()
    session.add(
        DashboardSession(
            team_member_id=member.id,
            token_hash=hash_token(session_token),
            expires_at=now + SESSION_TTL,
            last_seen_at=now,
        )
    )
    out = _session_out(member, member.business)
    await session.commit()
    _set_session_cookie(response, session_token, services)
    return DataOut(data=out)


@router.get("/auth/me", response_model=DataOut[SessionOut])
async def me(member: MemberDep) -> DataOut[SessionOut]:
    return DataOut(data=_session_out(member, member.business))


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(request: Request, session: SessionDep) -> Response:
    token = request.cookies.get(SESSION_COOKIE)
    if token:
        await session.execute(
            delete(DashboardSession).where(DashboardSession.token_hash == hash_token(token))
        )
        await session.commit()
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/team", response_model=DataOut[list[MemberOut]])
async def list_team(business: BusinessDep, session: SessionDep) -> DataOut[list[MemberOut]]:
    rows = (
        (
            await session.execute(
                select(TeamMember)
                .where(TeamMember.business_id == business.id)
                .order_by(TeamMember.created_at)
            )
        )
        .scalars()
        .all()
    )
    return DataOut(data=[MemberOut.model_validate(m) for m in rows])


@router.post("/team", response_model=DataOut[MemberOut], status_code=status.HTTP_201_CREATED)
async def invite_member(
    payload: InviteIn, member: MemberDep, session: SessionDep, services: ServicesDep
) -> DataOut[MemberOut]:
    """Owners add people by email; the invitee gets a sign-in link straight away."""
    if member.role != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only owners can add team members")
    email = str(payload.email).lower()
    business = member.business
    existing = await session.execute(
        select(TeamMember.id).where(
            TeamMember.business_id == business.id, TeamMember.email == email
        )
    )
    if existing.first() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "that person is already on the team")
    invitee = TeamMember(
        business_id=business.id, email=email, name=payload.name.strip(), role=MemberRole.MEMBER
    )
    session.add(invitee)
    await session.flush()
    link = _issue_login_token(session, services, invitee)
    subject, text = _login_email(invitee, business, link, invited_by=member.name or member.email)
    await session.commit()
    await session.refresh(invitee)
    out = MemberOut.model_validate(invitee)
    await _send(services, business, email, subject, text)
    if services.settings.is_dev:
        log.info("invite link for %s: %s", email, link)
    return DataOut(data=out)


@router.delete("/team/{member_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def remove_member(member_id: uuid.UUID, member: MemberDep, session: SessionDep) -> Response:
    """Owners remove people; their sessions and pending links go with them."""
    if member.role != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only owners can remove team members")
    if member_id == member.id:
        raise HTTPException(status.HTTP_409_CONFLICT, "you cannot remove yourself")
    target = (
        await session.execute(
            select(TeamMember).where(
                TeamMember.id == member_id, TeamMember.business_id == member.business_id
            )
        )
    ).scalar_one_or_none()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "team member not found")
    await session.delete(target)
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
