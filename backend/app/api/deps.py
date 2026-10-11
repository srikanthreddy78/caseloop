from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import Depends, Header, HTTPException, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import business_for_api_key, hash_api_key
from app.container import Services
from app.models import Business


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


async def require_business(
    session: SessionDep,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> Business:
    token = _bearer_token(authorization, x_api_key)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing API key")
    business = await business_for_api_key(session, token)
    if business is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid API key")
    return business


BusinessDep = Annotated[Business, Depends(require_business)]


class RateLimiter:
    """In-memory token bucket per key: ``per_minute`` requests of burst, refilled continuously.

    One instance per process, nothing shared across replicas: this guards against runaway
    widgets and scripts, it is not a billing meter.
    """

    def __init__(self, per_minute: int = 60) -> None:
        self.capacity = float(per_minute)
        self.refill_per_second = per_minute / 60
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

    def _prune(self, now: float) -> None:
        stale = now - 60
        for key, (_, last) in list(self._buckets.items()):
            if last < stale:
                del self._buckets[key]


limiter = RateLimiter(per_minute=60)


def _rate_limited() -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate limit exceeded, try again shortly",
        headers={"Retry-After": "1"},
    )


def _client_key(request: Request) -> str:
    return f"ip:{request.client.host if request.client else 'unknown'}"


async def rate_limit_api(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    x_api_key: Annotated[str | None, Header()] = None,
) -> None:
    """Per-business bucket for API-key endpoints; unauthenticated calls share a per-IP bucket."""
    token = _bearer_token(authorization, x_api_key)
    key = f"api:{hash_api_key(token)}" if token else _client_key(request)
    if not limiter.allow(key):
        raise _rate_limited()


async def rate_limit_widget(
    request: Request,
    x_widget_key: Annotated[str | None, Header()] = None,
) -> None:
    """Per-widget-key bucket for chat session creation; falls back to the client IP."""
    key = f"widget:{x_widget_key}" if x_widget_key else _client_key(request)
    if not limiter.allow(key):
        raise _rate_limited()


RateLimitApi = Depends(rate_limit_api)
RateLimitWidget = Depends(rate_limit_widget)
