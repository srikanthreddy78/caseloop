"""Operational commands: `python -m app.cli create-business ...`, `... add-owner ...`."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dev import create_business
from app.config import get_settings
from app.db import make_engine, make_session_factory
from app.models import Business, MemberRole, TeamMember
from app.schemas import CreateBusinessIn


async def _create_business(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    context = Path(args.context_file).read_text() if args.context_file else ""
    payload = CreateBusinessIn(
        name=args.name,
        slug=args.slug,
        inbound_email=args.inbound_email,
        from_email=args.from_email,
        escalation_email=args.escalation_email,
        context_text=context,
        dial_phone_number=args.dial_phone_number,
        dial_number_id=args.dial_number_id,
    )
    async with factory() as session:
        business, api_key = await create_business(session, payload)
    await engine.dispose()
    print(f"business_id={business.id}")
    print(f"api_key={api_key}   (shown once; store it now)")
    print(f"widget_public_key={business.widget_public_key}")
    print(f"inbound_email={business.inbound_email}")
    print(f"dashboard: sign in at {settings.base_url}/dashboard as {business.escalation_email}")
    return 0


async def add_owner(session: AsyncSession, slug: str, email: str) -> TeamMember:
    """Make ``email`` an owner of the business with ``slug``: added if new, promoted if not.

    The fallback for a business whose hand-off address nobody can read: an operator with
    database access gives a real person the dashboard.
    """
    email = email.strip().lower()
    business = (
        await session.execute(select(Business).where(Business.slug == slug))
    ).scalar_one_or_none()
    if business is None:
        raise LookupError(f"no business with slug {slug!r}")
    member = (
        await session.execute(
            select(TeamMember).where(
                TeamMember.business_id == business.id, TeamMember.email == email
            )
        )
    ).scalar_one_or_none()
    if member is None:
        member = TeamMember(business_id=business.id, email=email, role=MemberRole.OWNER)
        session.add(member)
    else:
        member.role = MemberRole.OWNER
    await session.commit()
    await session.refresh(member)
    return member


async def _add_owner(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = make_engine(settings.database_url)
    factory = make_session_factory(engine)
    try:
        async with factory() as session:
            member = await add_owner(session, args.slug, args.email)
    except LookupError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    print(f"owner={member.email} business_slug={args.slug}")
    print(f"dashboard: sign in at {settings.base_url}/dashboard as {member.email}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="caseloop")
    sub = parser.add_subparsers(dest="command", required=True)
    cb = sub.add_parser("create-business", help="Create a business and print its API key")
    cb.add_argument("--name", required=True)
    cb.add_argument("--slug", required=True)
    cb.add_argument("--inbound-email", required=True)
    cb.add_argument("--escalation-email", required=True)
    cb.add_argument("--from-email")
    cb.add_argument("--context-file", help="Text file with policies the agent must follow")
    cb.add_argument("--dial-phone-number")
    cb.add_argument("--dial-number-id")
    ao = sub.add_parser("add-owner", help="Give someone owner access to a business's dashboard")
    ao.add_argument("--slug", required=True)
    ao.add_argument("--email", required=True)
    args = parser.parse_args(argv)
    if args.command == "create-business":
        return asyncio.run(_create_business(args))
    if args.command == "add-owner":
        return asyncio.run(_add_owner(args))
    return 1


if __name__ == "__main__":
    sys.exit(main())
