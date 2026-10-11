"""Every business has someone who can sign in: at creation, by backfill, or by the CLI."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest
from app.cli import add_owner, main
from app.container import Services
from app.models import Business, TeamMember
from httpx import AsyncClient
from sqlalchemy import delete, select, text

from tests.conftest import BUSINESS_PAYLOAD
from tests.integration.test_auth import sign_in

MIGRATION = next(Path("alembic/versions").glob("b3c1d9e0a7f2_*.py"))


async def _members(services: Services, business_id: str) -> list[TeamMember]:
    async with services.session_factory() as session:
        rows = await session.execute(
            select(TeamMember).where(TeamMember.business_id == business_id)
        )
        return list(rows.scalars().all())


async def test_create_business_lowercases_the_owner(
    client: AsyncClient, services: Services, notifier: Any
) -> None:
    payload = dict(BUSINESS_PAYLOAD, escalation_email="Ops@Acme-Lamps.co")
    resp = await client.post("/dev/businesses", json=payload)
    assert resp.status_code == 201, resp.text
    business = resp.json()["data"]
    assert business["escalation_email"] == "ops@acme-lamps.co"
    members = await _members(services, business["id"])
    assert [(m.email, m.role) for m in members] == [("ops@acme-lamps.co", "owner")]
    session = await sign_in(client, notifier, "ops@acme-lamps.co")
    assert session["member"]["role"] == "owner"


async def test_backfill_gives_memberless_businesses_an_owner(
    business: dict[str, Any], services: Services
) -> None:
    spec = importlib.util.spec_from_file_location("backfill_owners", MIGRATION)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    async with services.session_factory() as session:
        await session.execute(delete(TeamMember))
        await session.execute(text("UPDATE businesses SET escalation_email = 'Ops@Acme-Lamps.co'"))
        await session.commit()
    assert await _members(services, business["id"]) == []

    async with services.session_factory() as session:
        await session.execute(text(module.OWNER_BACKFILL_SQL))
        await session.commit()
    members = await _members(services, business["id"])
    assert [(m.email, m.role, m.name) for m in members] == [("ops@acme-lamps.co", "owner", "")]

    # Businesses that already have a team are left alone.
    async with services.session_factory() as session:
        await session.execute(text(module.OWNER_BACKFILL_SQL))
        await session.commit()
    assert len(await _members(services, business["id"])) == 1


async def test_cli_add_owner_adds_or_promotes(
    client: AsyncClient, business: dict[str, Any], services: Services, notifier: Any
) -> None:
    owner = await sign_in(client, notifier, BUSINESS_PAYLOAD["escalation_email"])
    assert owner["member"]["role"] == "owner"
    invited = await client.post("/v1/team", json={"email": "sam@acme-lamps.co", "name": "Sam"})
    assert invited.status_code == 201 and invited.json()["data"]["role"] == "member"

    async with services.session_factory() as session:
        new = await add_owner(session, "acme", " Dana@Acme-Lamps.co ")
        promoted = await add_owner(session, "acme", "sam@acme-lamps.co")
        with pytest.raises(LookupError, match="no business with slug 'nope'"):
            await add_owner(session, "nope", "x@y.co")
    assert (new.email, new.role) == ("dana@acme-lamps.co", "owner")
    assert (promoted.email, promoted.role, promoted.name) == ("sam@acme-lamps.co", "owner", "Sam")
    assert len(await _members(services, business["id"])) == 3
    async with services.session_factory() as session:
        assert (await session.execute(select(Business.slug))).scalars().all() == ["acme"]


def test_cli_parses_add_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(args: Any) -> int:
        seen.update(vars(args))
        return 0

    monkeypatch.setattr("app.cli._add_owner", fake)
    assert main(["add-owner", "--slug", "acme", "--email", "x@y.co"]) == 0
    assert seen["slug"] == "acme" and seen["email"] == "x@y.co"
