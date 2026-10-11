from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business, DashboardSession

API_KEY_PREFIX = "cl_"
WIDGET_KEY_PREFIX = "pk_"

SESSION_COOKIE = "caseloop_session"
LOGIN_TOKEN_TTL = timedelta(minutes=15)
SESSION_TTL = timedelta(days=30)


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def generate_widget_key() -> str:
    return WIDGET_KEY_PREFIX + secrets.token_urlsafe(18)


def generate_login_token() -> str:
    return secrets.token_urlsafe(32)


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Tokens are stored hashed; a leaked table does not grant access."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_api_key(key: str) -> str:
    return hash_token(key)


def key_prefix(key: str) -> str:
    return key[:10]


async def business_for_api_key(session: AsyncSession, key: str) -> Business | None:
    if not key.startswith(API_KEY_PREFIX):
        return None
    result = await session.execute(
        select(Business).where(Business.api_key_hash == hash_api_key(key))
    )
    return result.scalar_one_or_none()


async def business_for_widget_key(session: AsyncSession, key: str) -> Business | None:
    result = await session.execute(select(Business).where(Business.widget_public_key == key))
    return result.scalar_one_or_none()


async def session_for_token(
    session: AsyncSession, token: str, now: datetime
) -> DashboardSession | None:
    """The live dashboard session behind a cookie value, or None if unknown or expired."""
    result = await session.execute(
        select(DashboardSession).where(
            DashboardSession.token_hash == hash_token(token), DashboardSession.expires_at > now
        )
    )
    return result.scalar_one_or_none()
