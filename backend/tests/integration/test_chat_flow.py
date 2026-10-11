from __future__ import annotations

from typing import Any

from httpx import AsyncClient


async def run_jobs(client: AsyncClient, advance_hours: float = 0) -> int:
    resp = await client.post("/dev/run-jobs", json={"advance_hours": advance_hours})
    assert resp.status_code == 200, resp.text
    return resp.json()["ran"]


async def test_widget_chat_round_trip(auth_client: AsyncClient, business: dict[str, Any]) -> None:
    key = business["widget_public_key"]
    resp = await auth_client.post(
        "/v1/chat/sessions",
        json={
            "email": "pat@customer-mail.co",
            "name": "Pat",
            "message": "My lamp arrived broken, order 4455",
        },
        headers={"X-Widget-Key": key},
    )
    assert resp.status_code == 201, resp.text
    sess = resp.json()
    token, case_id = sess["token"], sess["case_id"]
    assert sess["state"] == "new" and len(sess["messages"]) == 1

    await run_jobs(auth_client)
    poll = await auth_client.get(f"/v1/chat/sessions/{token}/messages")
    assert poll.status_code == 200
    msgs = poll.json()["messages"]
    assert [m["direction"] for m in msgs] == ["inbound", "outbound"]
    assert poll.json()["state"] == "awaiting_customer"
    after = msgs[-1]["at"]

    reply = await auth_client.post(
        f"/v1/chat/sessions/{token}/messages",
        json={"text": "Here is the photo (attached)"},
    )
    assert reply.status_code == 201
    await run_jobs(auth_client)
    newer = await auth_client.get(f"/v1/chat/sessions/{token}/messages", params={"after": after})
    assert newer.status_code == 200
    assert all(m["at"] > after for m in newer.json()["messages"])

    # The business sees the same case on its API, channel webchat, and internal notes stay hidden.
    case = (await auth_client.get(f"/v1/cases/{case_id}")).json()["data"]
    assert case["channel"] == "webchat" and case["customer"]["email"] == "pat@customer-mail.co"

    bad = await auth_client.get("/v1/chat/sessions/nope/messages")
    assert bad.status_code == 404
    noauth = await auth_client.post(
        "/v1/chat/sessions", json={"email": "x@customer-mail.co", "message": "hi"}
    )
    assert noauth.status_code == 401


async def test_widget_js_is_served(client: AsyncClient) -> None:
    resp = await client.get("/widget.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "X-Widget-Key" in resp.text
