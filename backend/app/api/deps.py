from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import SESSION_COOKIE, business_for_api_key, hash_api_key, session_for_token
from app.container import Services
from app.models import Business, DashboardSession, MemberRole, TeamMember

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


def _bearer_token(authorization: str | None, x_api_key: str | None) -> str | None:
    if x_api_key:
        return x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return None


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
    token = _bearer_token(authorization, x_api_key)
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


async def require_owner(
    request: Request, business: BusinessDep, session: SessionDep, services: ServicesDep
) -> TeamMember:
    """The signed-in owner of the business. Anything else that authenticates (an API key, a
    non-owner member) is refused with 403; no credentials at all is still 401."""
    dashboard_session = await resolve_dashboard_session(request, session, services)
    member = dashboard_session.member if dashboard_session is not None else None
    if member is None or member.business_id != business.id:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only a signed-in owner can do this")
    if member.role != MemberRole.OWNER:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "only owners can do this")
    return member


OwnerDep = Annotated[TeamMember, Depends(require_owner)]


class RateLimiter:
    """In-memory token bucket per key: ``limit`` requests of burst, refilled over ``window_seconds``.

    One instance per process, nothing shared across replicas: this guards against runaway
    widgets and scripts, it is not a billing meter.
    """

    def __init__(self, limit: int = 60, window_seconds: float = 60.0) -> None:
        self.capacity = float(limit)
        self.window_seconds = float(window_seconds)
        self.refill_per_second = limit / window_seconds
        self._buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, last seen)

    def allow(self, key: str, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.refill_per_second)
        if len(self._buckets) > 10_000:
            self._prune(now)
        if tokens < 1:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1, now)
        return True

    def reset(self) -> None:
        self._buckets.clear()

    def _prune(self, now: float) -> None:
        # A bucket idle for a whole window is full again, so dropping it changes nothing.
        stale = now - self.window_seconds
        for key, (_, last) in list(self._buckets.items()):
            if last < stale:
                del self._buckets[key]


class StreamSlots:
    """Counts open server-sent-event streams per key so one client cannot hold every worker.

    ``acquire`` returns False when any key is at its cap; every successful acquire must be paired
    with exactly one ``release`` (the stream's ``finally``).
    """

    def __init__(self, caps: dict[str, int]) -> None:
        self.caps = caps
        self._open: dict[tuple[str, str], int] = {}

    def acquire(self, keys: dict[str, str]) -> bool:
        for kind, key in keys.items():
            if self._open.get((kind, key), 0) >= self.caps[kind]:
                return False
        for kind, key in keys.items():
            self._open[(kind, key)] = self._open.get((kind, key), 0) + 1
        return True

    def release(self, keys: dict[str, str]) -> None:
        for kind, key in keys.items():
            left = self._open.get((kind, key), 0) - 1
            if left <= 0:
                self._open.pop((kind, key), None)
            else:
                self._open[(kind, key)] = left

    def count(self, kind: str, key: str) -> int:
        return self._open.get((kind, key), 0)

    def reset(self) -> None:
        self._open.clear()


# Per API key / dashboard session / client IP for the business API, and per client IP for the
# widget. A widget key is shared by every visitor of one site, so it gets a wider bucket of its own.
limiter = RateLimiter(limit=60)
widget_key_limiter = RateLimiter(limit=600)
# Sign-in links trigger an email each; keep the per-address rate low to stop inbox flooding.
magic_link_ip_limiter = RateLimiter(limit=10)
magic_link_email_limiter = RateLimiter(limit=3, window_seconds=600)
stream_slots = StreamSlots(caps={"token": 3, "ip": 20})


def reset_rate_limits() -> None:
    """Forget every bucket and open-stream count (tests)."""
    for lim in (limiter, widget_key_limiter, magic_link_ip_limiter, magic_link_email_limiter):
        lim.reset()
    stream_slots.reset()


def _rate_limited(retry_after: int = 1) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate limit exceeded, try again shortly",
        headers={"Retry-After": str(retry_after)},
    )


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _client_key(request: Request) -> str:
    return f"ip:{client_ip(request)}"


async def rate_limit_api(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """One bucket per API key or dashboard session; unauthenticated calls share a per-IP bucket."""
    token = _bearer_token(authorization, x_api_key)
    cookie = request.cookies.get(SESSION_COOKIE)
    if token:
        key = f"api:{hash_api_key(token)}"
    elif cookie:
        key = f"session:{hash_api_key(cookie)}"
    else:
        key = _client_key(request)
    if not limiter.allow(key):
        raise _rate_limited()


async def rate_limit_widget(
    request: Request,
    x_widget_key: Annotated[str | None, Header()] = None,
) -> None:
    """Two buckets for every widget call: the client IP (60/min) and, when sent, the widget key.

    Browsers cannot attach the key to EventSource or ``<img>`` requests, so those only count
    against the IP. Both buckets are debited even when one rejects, so a flood cannot probe
    the other for free.
    """
    ip_ok = limiter.allow(f"widget-ip:{client_ip(request)}")
    key_ok = widget_key_limiter.allow(f"widget:{x_widget_key}") if x_widget_key else True
    if not (ip_ok and key_ok):
        raise _rate_limited()


def check_magic_link_limits(request: Request, email: str) -> None:
    """10 link requests a minute per IP and 3 per address every 10 minutes."""
    ip_ok = magic_link_ip_limiter.allow(f"magic-ip:{client_ip(request)}")
    email_ok = magic_link_email_limiter.allow(f"magic-email:{email}")
    if not ip_ok:
        raise _rate_limited()
    if not email_ok:
        raise _rate_limited(retry_after=60)


RateLimitApi = Depends(rate_limit_api)
RateLimitWidget = Depends(rate_limit_widget)
