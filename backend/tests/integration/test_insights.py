from __future__ import annotations

from httpx import AsyncClient

from tests.integration.test_email_flow import run_jobs, simulate


async def test_insights_generated_from_recent_cases(auth_client: AsyncClient) -> None:
    none_yet = await auth_client.get("/v1/insights/latest")
    assert none_yet.status_code == 404

    await simulate(auth_client, "Lamp arrived cracked, order 1001", from_email="a@customer-mail.co")
    await simulate(auth_client, "Order 1002 never arrived", from_email="b@customer-mail.co")
    await simulate(
        auth_client,
        "Refund please for order 1003, the lamp is dim",
        from_email="c@customer-mail.co",
    )
    await run_jobs(auth_client)

    resp = await auth_client.post("/v1/insights/generate")
    assert resp.status_code == 201, resp.text
    snap = resp.json()["data"]
    assert snap["case_count"] == 3 and snap["model"] == "scripted"
    report = snap["report"]
    assert report["themes"] and sum(t["share_pct"] for t in report["themes"]) >= 90
    assert all(t["example_case_ids"] for t in report["themes"])

    latest = await auth_client.get("/v1/insights/latest")
    assert latest.status_code == 200 and latest.json()["data"]["id"] == snap["id"]
