from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

SessionFactory = async_sessionmaker[AsyncSession]


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(url, pool_pre_ping=True)


def make_session_factory(engine: AsyncEngine) -> SessionFactory:
    return async_sessionmaker(engine, expire_on_commit=False)


def sibling_database_url(url: str, suffix: str) -> str:
    """`.../caseloop` -> `.../caseloop_<suffix>`, keeping any query string."""
    base, _, query = url.partition("?")
    head, _, name = base.rpartition("/")
    return f"{head}/{name}_{suffix}" + (f"?{query}" if query else "")


@asynccontextmanager
async def scratch_database(url: str) -> AsyncIterator[AsyncEngine]:
    """A throwaway database for scripts: schema created on entry, every table emptied on exit.

    Refuses to run against a database that already holds businesses it did not create, so a
    script pointed at a real database by mistake stops before touching anything.
    """
    from app.models import Base

    engine = make_engine(url)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            existing = int(
                (await conn.execute(text("SELECT count(*) FROM businesses"))).scalar_one()
            )
    except Exception as exc:
        await engine.dispose()
        raise RuntimeError(
            f"cannot open scratch database {url}: {exc} (see make db-create)"
        ) from exc
    if existing:
        await engine.dispose()
        raise RuntimeError(
            f"{url} already holds {existing} business(es); scripts only run on an empty database"
        )
    try:
        yield engine
    finally:
        names = ", ".join(t.name for t in Base.metadata.sorted_tables)
        async with engine.begin() as conn:
            await conn.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))
        await engine.dispose()
