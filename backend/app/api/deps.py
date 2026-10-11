from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import SESSION_COOKIE, business_for_api_key, session_for_token
from app.container import Services
from app.models import Business, DashboardSession, TeamMember

LAST_SEEN_REFRESH = timedelta(minutes=5)


def get_services(request: Request) -> Services:
    services: Services = request.app.state.services
    return services


ServicesDep = Annotated[Services, Depends(get_services)]


async def get_session(services: ServicesDep) -> AsyncIterator[AsyncSession]:
    async with services.session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


SessionDep = Annotated[AsyncSession, Depends(get_session)]


async def resolve_dashboard_session(
    request: Request, session: AsyncSession, services: Services
) -> DashboardSession | None:
    """The dashboard session behind the cookie, if any. Looked up once per request."""
    if hasattr(request.state, "dashboard_session"):
        cached: DashboardSession | None = request.state.dashboard_session
        return cached
    token = request.cookies.get(SESSION_COOKIE)
    dashboard_session = None
    if token:
        now = services.clock.now()
        dashboard_session = await session_for_token(session, token, now)
        if dashboard_session is not None and (
            dashboard_session.last_seen_at is None
            or now - dashboard_session.last_seen_at > LAST_SEEN_REFRESH
        ):
            dashboard_session.last_seen_at = now
            await session.commit()
    request.state.dashboard_session = dashboard_session
    return dashboard_session


async def require_business(
    request: Request,
    session: SessionDep,
    services: ServicesDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Business:
    """API key (integrations) or dashboard session cookie (people); either resolves a business."""
    token = x_api_key
    if token is None and authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    if token:
        business = await business_for_api_key(session, token)
        if business is None:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
        return business
    dashboard_session = await resolve_dashboard_session(request, session, services)
    if dashboard_session is not None:
        return dashboard_session.member.business
    if request.cookies.get(SESSION_COOKIE):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired; sign in again")
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")


async def require_member(
    request: Request, session: SessionDep, services: ServicesDep
) -> TeamMember:
    """The signed-in person. Only a session cookie counts; API keys have no member."""
    dashboard_session = await resolve_dashboard_session(request, session, services)
    if dashboard_session is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "sign in required")
    return dashboard_session.member


BusinessDep = Annotated[Business, Depends(require_business)]
MemberDep = Annotated[TeamMember, Depends(require_member)]
