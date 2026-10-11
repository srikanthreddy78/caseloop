from __future__ import annotations

import hashlib
import secrets

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Business

API_KEY_PREFIX = "cl_"
WIDGET_KEY_PREFIX = "pk_"


def generate_api_key() -> str:
    return API_KEY_PREFIX + secrets.token_urlsafe(32)


def generate_widget_key() -> str:
    return WIDGET_KEY_PREFIX + secrets.token_urlsafe(18)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


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
