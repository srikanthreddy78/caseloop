"""Dashboard sign-in: magic links, cookie sessions, team management, API keys still work."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from app.clock import FakeClock
from app.notifications import FakeNotifier
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD

OWNER = "ops@acme-lamps.co"
LINK_RE = re.compile(r"/dashboard/login\?token=([A-Za-z0-9_-]+)")


def token_from_email(notifier: FakeNotifier, to: str) -> str:
    mails = [m for m in notifier.sent if m["to"] == to]
    assert mails, f"no email sent to {to}"
    match = LINK_RE.search(mails[-1]["text"])
    assert match, mails[-1]["text"]
    return match.group(1)


async def sign_in(client: AsyncClient, notifier: FakeNotifier, email: str) -> dict[str, Any]:
    """Request a link and exchange it. The client's cookie jar now holds this person's session."""
    resp = await client.post("/v1/auth/magic-link", json={"email": email})
    assert resp.status_code == 202, resp.text
    resp = await client.post(
        "/v1/auth/magic-link/exchange", json={"token": token_from_email(notifier, email)}
    )
    assert resp.status_code == 200, resp.text
    assert "caseloop_session" in resp.cookies
    data: dict[str, Any] = resp.json()["data"]
    return data


async def test_magic_link_round_trip(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    resp = await client.post("/v1/auth/magic-link", json={"email": OWNER.upper()})
    assert resp.status_code == 202, resp.text
    link = resp.json()["data"]["link"]  # returned in dev/test only
    assert link and link.startswith("http://test/dashboard/login?token=")

    assert len(notifier.sent) == 1
    mail = notifier.sent[0]
    assert mail["to"] == OWNER
    assert mail["from"] == business["from_email"]
    assert link in mail["text"]

    resp = await client.post(
        "/v1/auth/magic-link/exchange", json={"token": token_from_email(notifier, OWNER)}
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["member"]["email"] == OWNER
    assert data["member"]["role"] == "owner"
    assert data["business"]["id"] == business["id"]
    set_cookie = resp.headers["set-cookie"].lower()
    assert "caseloop_session=" in set_cookie
    assert "httponly" in set_cookie
    assert "samesite=lax" in set_cookie
    assert "max-age=2592000" in set_cookie

    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["member"]["email"] == OWNER

    # The cookie alone grants access to the business API; no Authorization header is sent.
    resp = await client.get("/v1/cases")
    assert resp.status_code == 200, resp.text
    resp = await client.get("/v1/businesses/me")
    assert resp.status_code == 200
    assert resp.json()["data"]["id"] == business["id"]


async def test_unknown_email_is_accepted_but_nothing_is_sent(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    resp = await client.post("/v1/auth/magic-link", json={"email": "nobody@example.net"})
    assert resp.status_code == 202
    assert resp.json()["data"]["link"] is None
    assert notifier.sent == []


async def test_used_token_is_rejected(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    await sign_in(client, notifier, OWNER)
    token = token_from_email(notifier, OWNER)
    resp = await client.post("/v1/auth/magic-link/exchange", json={"token": token})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "unauthorized"


async def test_concurrent_exchanges_of_one_link_yield_one_session(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    resp = await client.post("/v1/auth/magic-link", json={"email": OWNER})
    assert resp.status_code == 202
    token = token_from_email(notifier, OWNER)
    # Each exchange gets its own cookie jar; only one may claim the token.
    attempts = [AsyncClient(transport=client._transport, base_url="http://test") for _ in range(4)]
    try:
        results = await asyncio.gather(
            *(c.post("/v1/auth/magic-link/exchange", json={"token": token}) for c in attempts)
        )
    finally:
        for c in attempts:
            await c.aclose()
    assert sorted(r.status_code for r in results) == [200, 401, 401, 401], [r.text for r in results]
    winner = next(r for r in results if r.status_code == 200)
    assert "caseloop_session" in winner.cookies


async def test_member_emails_are_stored_lowercase(
    client: AsyncClient, notifier: FakeNotifier
) -> None:
    resp = await client.post(
        "/dev/businesses",
        json={
            **BUSINESS_PAYLOAD,
            "escalation_email": "Ops@Acme-Lamps.CO",
            "dial_phone_number": None,
        },
    )
    assert resp.status_code == 201, resp.text
    resp = await client.post("/v1/auth/magic-link", json={"email": "OPS@acme-lamps.co"})
    assert resp.status_code == 202
    resp = await client.post(
        "/v1/auth/magic-link/exchange",
        json={"token": token_from_email(notifier, "ops@acme-lamps.co")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["member"]["email"] == "ops@acme-lamps.co"
    resp = await client.post("/v1/team", json={"email": "  Jo@Acme-Lamps.CO ", "name": "Jo"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["data"]["email"] == "jo@acme-lamps.co"
    emails = [m["email"] for m in (await client.get("/v1/team")).json()["data"]]
    assert emails == ["ops@acme-lamps.co", "jo@acme-lamps.co"]


async def test_expired_token_is_rejected(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier, clock: FakeClock
) -> None:
    resp = await client.post("/v1/auth/magic-link", json={"email": OWNER})
    assert resp.status_code == 202
    clock.advance(minutes=16)
    resp = await client.post(
        "/v1/auth/magic-link/exchange", json={"token": token_from_email(notifier, OWNER)}
    )
    assert resp.status_code == 401
    resp = await client.post("/v1/auth/magic-link/exchange", json={"token": "not-a-token"})
    assert resp.status_code == 401


async def test_session_expires_after_thirty_days(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier, clock: FakeClock
) -> None:
    await sign_in(client, notifier, OWNER)
    clock.advance(days=29)
    assert (await client.get("/v1/auth/me")).status_code == 200
    clock.advance(days=2)
    assert (await client.get("/v1/auth/me")).status_code == 401
    resp = await client.get("/v1/cases")
    assert resp.status_code == 401
    assert "expired" in resp.json()["error"]["message"]


async def test_logout_ends_the_session(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    await sign_in(client, notifier, OWNER)
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 204
    assert "caseloop_session=" in resp.headers["set-cookie"]
    assert (await client.get("/v1/auth/me")).status_code == 401
    assert (await client.get("/v1/cases")).status_code == 401


async def test_no_credentials_is_unauthorized(
    client: AsyncClient, business: dict[str, Any]
) -> None:
    resp = await client.get("/v1/cases")
    assert resp.status_code == 401
    assert (await client.get("/v1/auth/me")).status_code == 401
    assert (await client.get("/v1/team")).status_code == 401


async def test_owner_invites_and_removes_members(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    await sign_in(client, notifier, OWNER)
    resp = await client.post("/v1/team", json={"email": "Jo@Acme-Lamps.co", "name": "Jo"})
    assert resp.status_code == 201, resp.text
    member = resp.json()["data"]
    assert member["email"] == "jo@acme-lamps.co"
    assert member["role"] == "member"
    assert member["name"] == "Jo"

    invite = [m for m in notifier.sent if m["to"] == "jo@acme-lamps.co"]
    assert len(invite) == 1
    assert "added you to Acme Lamps" in invite[0]["subject"]
    assert LINK_RE.search(invite[0]["text"])

    resp = await client.post("/v1/team", json={"email": "jo@acme-lamps.co"})
    assert resp.status_code == 409

    resp = await client.get("/v1/team")
    assert resp.status_code == 200
    emails = [m["email"] for m in resp.json()["data"]]
    assert emails == [OWNER, "jo@acme-lamps.co"]

    resp = await client.delete(f"/v1/team/{member['id']}")
    assert resp.status_code == 204
    emails = [m["email"] for m in (await client.get("/v1/team")).json()["data"]]
    assert emails == [OWNER]

    # The invitee's link no longer works once they are removed.
    resp = await client.post(
        "/v1/auth/magic-link/exchange",
        json={"token": token_from_email(notifier, "jo@acme-lamps.co")},
    )
    assert resp.status_code == 401

    me = (await client.get("/v1/auth/me")).json()["data"]["member"]
    assert (await client.delete(f"/v1/team/{me['id']}")).status_code == 409


async def test_non_owner_cannot_invite_or_remove(
    client: AsyncClient, business: dict[str, Any], notifier: FakeNotifier
) -> None:
    owner = await sign_in(client, notifier, OWNER)
    resp = await client.post("/v1/team", json={"email": "jo@acme-lamps.co", "name": "Jo"})
    assert resp.status_code == 201
    jo_id = resp.json()["data"]["id"]

    # Jo signs in with the invite link; the cookie jar now holds Jo's session.
    resp = await client.post(
        "/v1/auth/magic-link/exchange",
        json={"token": token_from_email(notifier, "jo@acme-lamps.co")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["member"]["role"] == "member"
    assert (await client.get("/v1/auth/me")).json()["data"]["member"]["id"] == jo_id

    # Jo can see cases and the team, but cannot change the team.
    assert (await client.get("/v1/cases")).status_code == 200
    assert (await client.get("/v1/team")).status_code == 200
    resp = await client.post("/v1/team", json={"email": "sam@acme-lamps.co"})
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "forbidden"
    resp = await client.delete(f"/v1/team/{owner['member']['id']}")
    assert resp.status_code == 403


async def test_api_key_still_works(auth_client: AsyncClient, business: dict[str, Any]) -> None:
    resp = await auth_client.get("/v1/cases")
    assert resp.status_code == 200, resp.text
    resp = await auth_client.get("/v1/team")
    assert resp.status_code == 200
    assert [m["role"] for m in resp.json()["data"]] == ["owner"]
    # Team changes and /me need a person, not an integration key.
    assert (await auth_client.get("/v1/auth/me")).status_code == 401
    resp = await auth_client.post("/v1/team", json={"email": "jo@acme-lamps.co"})
    assert resp.status_code == 401
    # A bad key is still rejected even if a cookie is present.
    auth_client.headers["Authorization"] = "Bearer cl_wrong"
    assert (await auth_client.get("/v1/cases")).status_code == 401
