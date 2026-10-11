from __future__ import annotations

from typing import Any

from app.channels.registry import ChannelRegistry
from app.clock import FakeClock
from app.notifications import FakeNotifier
from httpx import AsyncClient

from tests.conftest import BUSINESS_PAYLOAD, email_adapter

INBOUND = BUSINESS_PAYLOAD["inbound_email"]


async def simulate(client: AsyncClient, text: str, **extra: Any) -> dict[str, Any]:
    payload = {
        "to": INBOUND,
        "from_email": "jane@customer-mail.co",
        "from_name": "Jane",
        "subject": "Broken lamp",
        "text": text,
    }
    payload.update(extra)
    resp = await client.post("/dev/simulate/email", json=payload)
    assert resp.status_code == 202, resp.text
    return resp.json()["data"]


async def run_jobs(client: AsyncClient, advance_hours: float = 0) -> int:
    resp = await client.post("/dev/run-jobs", json={"advance_hours": advance_hours})
    assert resp.status_code == 200, resp.text
    return resp.json()["ran"]


async def get_case(client: AsyncClient, case_id: str) -> dict[str, Any]:
    resp = await client.get(f"/v1/cases/{case_id}")
    assert resp.status_code == 200, resp.text
    return resp.json()["data"]


async def test_complaint_is_gathered_then_resolved(
    auth_client: AsyncClient, channels: ChannelRegistry
) -> None:
    case = await simulate(
        auth_client, "Hi, my lamp arrived cracked and the shade is dented. Please help."
    )
    assert case["state"] == "new"

    assert await run_jobs(auth_client) == 1
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "awaiting_customer"
    assert case["category"] == "damaged"
    missing = {r["key"] for r in case["requirements"] if r["status"] == "missing"}
    assert missing == {"order_number", "photos"}
    sent = email_adapter(channels).sent
    assert (
        len(sent) == 1
        and "order number" in sent[0].text.lower()
        and sent[0].to == "jane@customer-mail.co"
    )

    await simulate(
        auth_client,
        "Sure, it's order #A12345. Photos attached.",
        attachments=[{"filename": "crack.jpg", "content_type": "image/jpeg"}],
    )
    assert await run_jobs(auth_client) == 1
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "resolved"
    assert case["resolution"]["type"] == "replacement_sent"
    assert {r["status"] for r in case["requirements"]} == {"provided"}
    assert len(email_adapter(channels).sent) == 2

    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    types = [t.get("type") for t in timeline if t["kind"] == "event"]
    assert "case_created" in types and "case_assessed" in types and "state_changed" in types


async def test_followups_then_abandon_and_notify(
    auth_client: AsyncClient, channels: ChannelRegistry, clock: FakeClock, notifier: FakeNotifier
) -> None:
    case = await simulate(auth_client, "My order never arrived.")
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "awaiting_customer" and case["followups_sent"] == 0
    adapter = email_adapter(channels)
    assert len(adapter.sent) == 1

    # Nothing is due before the first window elapses.
    assert await run_jobs(auth_client, advance_hours=23) == 0
    assert await run_jobs(auth_client, advance_hours=2) >= 1
    case = await get_case(auth_client, case["id"])
    assert case["followups_sent"] == 1 and len(adapter.sent) == 2
    assert "checking in" in adapter.sent[1].text.lower()

    await run_jobs(auth_client, advance_hours=73)
    await run_jobs(auth_client, advance_hours=169)
    case = await get_case(auth_client, case["id"])
    assert case["followups_sent"] == 3 and case["state"] == "awaiting_customer"
    assert len(adapter.sent) == 4

    await run_jobs(auth_client, advance_hours=169)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "abandoned"
    assert case["escalation"]["reason"] == "customer_unresponsive"
    assert len(notifier.sent) == 1
    assert notifier.sent[0]["to"] == BUSINESS_PAYLOAD["escalation_email"]
    assert "Needs a human" in notifier.sent[0]["subject"]


async def test_customer_reply_cancels_pending_followup(
    auth_client: AsyncClient, channels: ChannelRegistry
) -> None:
    case = await simulate(auth_client, "I want a refund for order 77001, the lamp flickers.")
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    # order number was extracted on the first pass, so only the ask for nothing else -> resolved
    assert case["state"] == "resolved"


async def test_reply_before_followup_window(
    auth_client: AsyncClient, channels: ChannelRegistry
) -> None:
    case = await simulate(auth_client, "The box arrived smashed.")
    await run_jobs(auth_client)
    await simulate(
        auth_client,
        "Order is 55501, photo attached",
        attachments=[{"filename": "box.png", "content_type": "image/png"}],
    )
    await run_jobs(auth_client, advance_hours=1)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "resolved"
    # The old follow-up job must not fire a nudge later.
    assert await run_jobs(auth_client, advance_hours=48) == 0
    assert len(email_adapter(channels).sent) == 2


async def test_legal_threat_escalates(auth_client: AsyncClient, notifier: FakeNotifier) -> None:
    case = await simulate(
        auth_client, "This is unacceptable, order 9001 is broken and I will get my lawyer."
    )
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "escalated"
    assert case["escalation"]["reason"] == "legal_threat"
    assert len(notifier.sent) == 1


async def test_resolved_case_reopens_on_new_message(auth_client: AsyncClient) -> None:
    case = await simulate(auth_client, "Refund please, order 31337 is faulty.")
    await run_jobs(auth_client)
    assert (await get_case(auth_client, case["id"]))["state"] == "resolved"
    again = await simulate(auth_client, "Actually it is still not working after the fix.")
    assert again["id"] == case["id"]
    assert again["state"] == "gathering"


async def test_tenant_isolation_and_auth(client: AsyncClient, business: dict[str, Any]) -> None:
    other = dict(
        BUSINESS_PAYLOAD,
        slug="other",
        inbound_email="help@other-shop.co",
        dial_phone_number="+15550002222",
    )
    resp = await client.post("/dev/businesses", json=other)
    assert resp.status_code == 201
    other_key = resp.json()["data"]["api_key"]

    case = await simulate(client, "order 1 broken")
    mine = await client.get(
        f"/v1/cases/{case['id']}", headers={"Authorization": f"Bearer {business['api_key']}"}
    )
    assert mine.status_code == 200
    theirs = await client.get(
        f"/v1/cases/{case['id']}", headers={"Authorization": f"Bearer {other_key}"}
    )
    assert theirs.status_code == 404
    anon = await client.get("/v1/cases")
    assert anon.status_code == 401 and anon.json()["error"]["code"] == "unauthorized"
    bad = await client.get("/v1/cases", headers={"X-API-Key": "cl_nope"})
    assert bad.status_code == 401


async def test_create_case_via_api_and_human_actions(
    auth_client: AsyncClient, channels: ChannelRegistry
) -> None:
    resp = await auth_client.post(
        "/v1/cases",
        json={
            "customer_email": "sam@customer-mail.co",
            "customer_name": "Sam",
            "text": "Double charged this month",
            "channel": "api",
        },
    )
    assert resp.status_code == 201 and resp.headers["location"].startswith("/v1/cases/")
    case = resp.json()["data"]
    assert case["state"] == "new" and case["channel"] == "api"

    listed = await auth_client.get("/v1/cases", params={"state": "new,gathering"})
    assert listed.status_code == 200 and listed.json()["meta"]["total"] == 1

    await run_jobs(auth_client)
    reply = await auth_client.post(
        f"/v1/cases/{case['id']}/messages", json={"text": "Looking into it now."}
    )
    assert reply.status_code == 201
    assert channels.adapters["api"].sent[-1].text == "Looking into it now."  # type: ignore[attr-defined]

    done = await auth_client.post(
        f"/v1/cases/{case['id']}/resolve", json={"resolution_type": "credit_issued", "notes": "ok"}
    )
    assert done.status_code == 200 and done.json()["data"]["state"] == "resolved"
    again = await auth_client.post(f"/v1/cases/{case['id']}/resolve", json={})
    assert again.status_code == 409


async def test_delivery_failure_is_recorded_not_fatal(
    auth_client: AsyncClient, channels: ChannelRegistry, clock: FakeClock
) -> None:
    from app.channels.base import ChannelUnavailable

    class Broken:
        channel = "email"

        async def send(self, **kwargs: Any) -> None:
            raise ChannelUnavailable("domain not verified")

    working = channels.adapters["email"]
    channels.adapters["email"] = Broken()  # type: ignore[assignment]
    case = await simulate(auth_client, "The lamp arrived smashed.")
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    # The customer never saw the question, so nothing is asked and nobody is nudged: a person
    # has to resend it.
    assert case["state"] == "escalated", case["state"]
    assert case["escalation"]["reason"] == "delivery_failed"
    assert "domain not verified" in case["escalation"]["summary"]
    assert case["last_agent_message_at"] is None
    assert all(r["asked_count"] == 0 for r in case["requirements"])
    outbound = [m for m in case["messages"] if m["direction"] == "outbound"]
    assert len(outbound) == 1 and outbound[0]["meta"]["delivery"] == "failed"
    assert "domain not verified" in outbound[0]["meta"]["error"]
    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    assert any(t.get("type") == "delivery_failed" for t in timeline)
    assert not any(t.get("type") == "followup_scheduled" for t in timeline)
    tool_results = [t["data"]["result"] for t in timeline if t.get("type") == "tool_called"]
    assert any('"delivered": false' in r and "delivery_failed" in r for r in tool_results)

    # Still broken: the resend is refused and the message stays flagged.
    resend = f"/v1/cases/{case['id']}/messages/{outbound[0]['id']}/resend"
    assert (await auth_client.post(resend)).status_code == 502
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "escalated"
    assert case["messages"][-1]["meta"]["delivery"] == "failed"

    # Channel fixed: the resend delivers, the case goes back to the agent with a follow-up.
    channels.adapters["email"] = working
    resp = await auth_client.post(resend)
    assert resp.status_code == 200, resp.text
    case = resp.json()["data"]
    assert case["state"] == "awaiting_customer" and case["escalation"] is None
    assert case["last_agent_message_at"] is not None
    assert all(r["asked_count"] == 1 for r in case["requirements"] if r["status"] == "missing")
    outbound = [m for m in case["messages"] if m["direction"] == "outbound"]
    assert len(outbound) == 1 and outbound[0]["meta"]["delivery"] == "sent"
    assert len(email_adapter(channels).sent) == 1
    timeline = (await auth_client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
    assert any(t.get("type") == "message_sent" and t["data"].get("resent") for t in timeline)
    assert any(t.get("type") == "followup_scheduled" and t["data"]["hours"] == 24 for t in timeline)
    assert (await auth_client.post(resend)).status_code == 409  # already delivered

    assert await run_jobs(auth_client, advance_hours=23) == 0
    assert await run_jobs(auth_client, advance_hours=2) >= 1
    case = await get_case(auth_client, case["id"])
    assert case["followups_sent"] == 1 and len(email_adapter(channels).sent) == 2


async def test_human_reply_that_fails_is_saved_and_resendable(
    auth_client: AsyncClient, channels: ChannelRegistry
) -> None:
    from app.channels.base import ChannelUnavailable

    class Broken:
        channel = "email"

        async def send(self, **kwargs: Any) -> None:
            raise ChannelUnavailable("domain not verified")

    case = await simulate(auth_client, "The lamp arrived smashed.")
    await run_jobs(auth_client)
    working = channels.adapters["email"]
    channels.adapters["email"] = Broken()  # type: ignore[assignment]
    resp = await auth_client.post(f"/v1/cases/{case['id']}/messages", json={"text": "On it."})
    assert resp.status_code == 502
    assert "not delivered" in resp.json()["error"]["message"]
    case = await get_case(auth_client, case["id"])
    assert case["state"] == "awaiting_customer"
    failed = case["messages"][-1]
    assert failed["sender"] == "human" and failed["meta"]["delivery"] == "failed"

    channels.adapters["email"] = working
    resp = await auth_client.post(f"/v1/cases/{case['id']}/messages/{failed['id']}/resend")
    assert resp.status_code == 200, resp.text
    case = resp.json()["data"]
    assert case["state"] == "awaiting_customer"  # was never escalated; nothing to reschedule
    assert case["messages"][-1]["meta"]["delivery"] == "sent"
    assert email_adapter(channels).sent[-1].text == "On it."
    bogus = f"/v1/cases/{case['id']}/messages/{case['messages'][0]['id']}/resend"
    assert (await auth_client.post(bogus)).status_code == 404  # inbound message


async def test_facts_given_up_front_become_provided_requirements(auth_client: AsyncClient) -> None:
    case = await simulate(
        auth_client, "Refund please for order 31337, delivered last week and it flickers."
    )
    await run_jobs(auth_client)
    case = await get_case(auth_client, case["id"])
    req = next(r for r in case["requirements"] if r["key"] == "order_number")
    assert req["status"] == "provided" and req["value"] == "31337"
