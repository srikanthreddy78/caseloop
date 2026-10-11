"""End-to-end demo: a damaged-item complaint handled start to finish, in-process, no network.

uv run python scripts/demo.py          # scripted agent, fake channels
uv run python scripts/demo.py --live   # real OpenAI model drives the tools; channels still fake
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from typing import Any

from app.channels.fake import FakeAdapter
from app.channels.registry import ChannelRegistry
from app.clock import FakeClock
from app.config import Settings
from app.container import build_agent, build_services
from app.db import scratch_database, sibling_database_url
from app.main import create_app
from app.notifications import FakeNotifier
from httpx import ASGITransport, AsyncClient

POLICY = """Acme Lamps support policy:
- Damaged on arrival: we send a free replacement once we have the order number and a photo of the damage.
- Refunds: within 30 days of delivery, need the order number. Outside 30 days, offer store credit only.
- Never promise expedited shipping. Escalate anything involving legal threats or amounts over $500.
"""


def say(label: str, text: str) -> None:
    print(f"\n\033[1m{label}\033[0m\n{text}")


async def main(live: bool) -> int:
    settings = Settings(
        env="dev", scheduler_enabled=False, agent_runner="openai" if live else "scripted"
    )
    # Never the configured database: a fake clock would fire real businesses' follow-ups.
    settings.database_url = sibling_database_url(settings.database_url, "scratch")
    clock = FakeClock()
    channels = ChannelRegistry.all_fake()
    notifier = FakeNotifier()
    if live and not settings.openai_api_key:
        print("OPENAI_API_KEY is not set; cannot run --live", file=sys.stderr)
        return 2
    runner, assessor = build_agent(settings)
    async with scratch_database(settings.database_url) as engine:
        services = build_services(
            settings,
            engine=engine,
            clock=clock,
            channels=channels,
            notifier=notifier,
            runner=runner,
            assessor=assessor,
        )
        return await run_demo(services, channels, notifier)


async def run_demo(services: Any, channels: ChannelRegistry, notifier: FakeNotifier) -> int:
    app = create_app(services)
    app.state.services = services
    email = channels.adapters["email"]
    assert isinstance(email, FakeAdapter)

    slug = f"acme-demo-{uuid.uuid4().hex[:8]}"
    inbound = f"support@{slug}.caseloop.dev"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://demo") as client:
        biz = (
            await client.post(
                "/dev/businesses",
                json={
                    "name": "Acme Lamps",
                    "slug": slug,
                    "inbound_email": inbound,
                    "escalation_email": "ops@acme-lamps.co",
                    "context_text": POLICY,
                },
            )
        ).json()["data"]
        client.headers["Authorization"] = f"Bearer {biz['api_key']}"
        say("Agent", f"{services.runner.name} runner, assessor {services.assessor.name}")

        async def inbound_email(text: str, **extra: Any) -> dict[str, Any]:
            payload = {
                "to": inbound,
                "from_email": "jane@customer-mail.co",
                "from_name": "Jane",
                "subject": "Broken lamp",
                "text": text,
            }
            payload.update(extra)
            resp = await client.post("/dev/simulate/email", json=payload)
            resp.raise_for_status()
            return resp.json()["data"]

        async def run(advance_hours: float = 0) -> None:
            resp = await client.post(
                "/dev/run-jobs", json={"advance_hours": advance_hours, "business_id": biz["id"]}
            )
            resp.raise_for_status()

        async def show(case_id: str) -> dict[str, Any]:
            case = (await client.get(f"/v1/cases/{case_id}")).json()["data"]
            reqs = (
                ", ".join(f"{r['key']}={r['status']}" for r in case["requirements"]) or "(none yet)"
            )
            say(
                "Case",
                f"state={case['state']} category={case['category']} followups={case['followups_sent']}\nrequirements: {reqs}",
            )
            return case

        complaint = "Hi, the lamp I ordered arrived with a cracked base and a dented shade. I'd like a replacement."
        say("Customer (email)", complaint)
        case = await inbound_email(complaint)
        await run()
        await show(case["id"])
        say("Agent (email)", email.sent[-1].text)

        await run(advance_hours=25)
        await show(case["id"])
        say("Agent (nudge after 24h)", email.sent[-1].text)

        reply = "Sorry for the delay! Order number is A12345. Photo of the crack attached."
        say("Customer (email)", reply)
        await inbound_email(
            reply, attachments=[{"filename": "crack.jpg", "content_type": "image/jpeg"}]
        )
        await run()
        case = await show(case["id"])
        say("Agent (email)", email.sent[-1].text)
        say("Resolution", json.dumps(case["resolution"], indent=2))

        timeline = (await client.get(f"/v1/cases/{case['id']}/timeline")).json()["data"]
        say(
            "Timeline",
            "\n".join(
                f"{t['at'][:16]}  {t.get('type') or t.get('direction')}  {t.get('text', '')[:70] if t['kind'] == 'message' else json.dumps(t.get('data'))[:90]}"
                for t in timeline
            ),
        )
        say("Escalation emails sent to the business", str(len(notifier.sent)))
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="use the real OpenAI model")
    args = parser.parse_args()
    try:
        sys.exit(asyncio.run(main(args.live)))
    except RuntimeError as exc:  # scratch database missing or not empty
        sys.exit(f"demo: {exc}")
