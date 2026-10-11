"""Run the agent evaluation set (evals/cases.yaml) in-process: fake channels, fake clock, no network.

uv run python scripts/eval.py                  # scripted runner (deterministic baseline)
uv run python scripts/eval.py --live           # real OpenAI runner drives the tools (costs money)
uv run python scripts/eval.py --min-pass 0.8   # exit 1 when the pass rate is below this
uv run python scripts/eval.py --only refund    # run the cases whose name contains this

Every case gets a fresh business, its stored orders, and the scripted customer messages; the
case is then checked against the expectations in the YAML and the business is deleted again.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from app.channels.registry import ChannelRegistry
from app.clock import FakeClock
from app.config import Settings
from app.container import build_agent, build_services
from app.main import create_app
from app.models import Business, CaseState
from app.notifications import FakeNotifier
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete

CASES_FILE = Path(__file__).resolve().parents[1] / "evals" / "cases.yaml"
DEFAULT_MIN_PASS = 0.6  # the scripted runner's floor; `make eval-live` asks for 0.8
STATES = {s.value for s in CaseState}
DATE_FIELDS = ("ordered_at", "shipped_at", "delivered_at")


@dataclass
class Outcome:
    name: str
    passed: bool
    problems: list[str] = field(default_factory=list)
    state: str = "?"
    seconds: float = 0


def load_cases(path: Path, only: str | None) -> tuple[str, list[dict[str, Any]]]:
    doc = yaml.safe_load(path.read_text())
    policy = doc.get("policy")
    cases = doc.get("cases")
    if not isinstance(policy, str) or not isinstance(cases, list) or not cases:
        raise ValueError(f"{path}: need a top-level `policy` string and a non-empty `cases` list")
    names: set[str] = set()
    for case in cases:
        validate_case(case)
        if case["name"] in names:
            raise ValueError(f"duplicate case name {case['name']!r}")
        names.add(case["name"])
    if only:
        cases = [c for c in cases if only in c["name"]]
    return policy, cases


def validate_case(case: Any) -> None:
    name = case.get("name") if isinstance(case, dict) else None
    if not isinstance(name, str) or not name:
        raise ValueError(f"case without a name: {case!r}")

    def fail(msg: str) -> ValueError:
        return ValueError(f"case {name!r}: {msg}")

    customer = case.get("customer")
    if not isinstance(customer, dict) or not (customer.get("email") or customer.get("phone")):
        raise fail("customer needs an email or a phone")
    if case.get("channel", "email") not in ("email", "sms"):
        raise fail("channel must be email or sms")
    if case.get("channel") == "sms" and not customer.get("phone"):
        raise fail("sms cases need customer.phone")
    steps = case.get("steps")
    if not isinstance(steps, list) or not steps:
        raise fail("steps must be a non-empty list")
    for step in steps:
        if not isinstance(step, dict) or not (step.get("text") or step.get("advance_hours")):
            raise fail(f"step needs text and/or advance_hours: {step!r}")
    for order in case.get("orders") or []:
        if not isinstance(order, dict) or not order.get("order_number"):
            raise fail(f"order needs an order_number: {order!r}")
    expect = case.get("expect")
    if not isinstance(expect, dict):
        raise fail("expect is required")
    states = expect.get("state")
    states = states if isinstance(states, list) else [states]
    if not states or any(s not in STATES for s in states):
        raise fail(f"expect.state must be one of {sorted(STATES)}, got {states!r}")
    if not isinstance(expect.get("provided", []), list):
        raise fail("expect.provided must be a list of requirement keys")
    if not isinstance(expect.get("escalated", False), bool):
        raise fail("expect.escalated must be true or false")
    contains = expect.get("reply_contains", [])
    if not isinstance(contains, list) or len(contains) > 3 or not all(contains):
        raise fail("expect.reply_contains must be a list of 0-3 non-empty substrings")


def order_rows(orders: list[dict[str, Any]], now: datetime) -> list[dict[str, Any]]:
    """Turn `<field>_days_ago` into ISO dates relative to the clock so windows stay checkable."""
    rows = []
    for order in orders:
        row = {k: v for k, v in order.items() if not k.endswith("_days_ago")}
        for prefix in ("ordered", "shipped", "delivered"):
            days = order.get(f"{prefix}_days_ago")
            if days is not None:
                row[f"{prefix}_at"] = (now - timedelta(days=float(days))).date().isoformat()
        rows.append({"order_number": str(row.pop("order_number")), **row})
    return rows


class Harness:
    def __init__(self, client: AsyncClient, services: Any, clock: FakeClock) -> None:
        self.client = client
        self.services = services
        self.clock = clock

    async def run_case(self, case: dict[str, Any], default_policy: str) -> Outcome:
        started = time.perf_counter()
        business = await self._create_business(case, default_policy)
        self.client.headers["Authorization"] = f"Bearer {business['api_key']}"
        try:
            if case.get("orders"):
                resp = await self.client.post(
                    "/v1/orders",
                    json={"orders": order_rows(case["orders"], self.clock.now()), "source": "api"},
                )
                resp.raise_for_status()
            case_id = await self._play(case, business)
            detail = (await self.client.get(f"/v1/cases/{case_id}")).json()["data"]
            outcome = judge(case, detail)
        finally:
            self.client.headers.pop("Authorization", None)
            await self._delete_business(business["id"])
        outcome.seconds = time.perf_counter() - started
        return outcome

    async def _create_business(self, case: dict[str, Any], default_policy: str) -> dict[str, Any]:
        slug = f"eval-{uuid.uuid4().hex[:10]}"
        resp = await self.client.post(
            "/dev/businesses",
            json={
                "name": "Acme Lamps",
                "slug": slug,
                "inbound_email": f"support@{slug}.caseloop.dev",
                "escalation_email": "ops@acme-lamps.co",
                "context_text": case.get("policy") or default_policy,
                "dial_phone_number": f"+1555{uuid.uuid4().int % 10_000_000:07d}",
                "dial_number_id": f"num_{slug}",
            },
        )
        resp.raise_for_status()
        return resp.json()["data"]

    async def _delete_business(self, business_id: str) -> None:
        async with self.services.session_factory() as session:
            await session.execute(delete(Business).where(Business.id == uuid.UUID(business_id)))
            await session.commit()

    async def _play(self, case: dict[str, Any], business: dict[str, Any]) -> str:
        customer = case["customer"]
        case_id: str | None = None
        for step in case["steps"]:
            hours = float(step.get("advance_hours") or 0)
            if hours:
                await self._run_jobs(hours)
            if not step.get("text"):
                continue
            if case.get("channel") == "sms":
                resp = await self.client.post(
                    "/dev/simulate/sms",
                    json={
                        "to": business["dial_phone_number"],
                        "from_phone": customer["phone"],
                        "body": step["text"],
                    },
                )
            else:
                resp = await self.client.post(
                    "/dev/simulate/email",
                    json={
                        "to": business["inbound_email"],
                        "from_email": customer["email"],
                        "from_name": customer.get("name"),
                        "subject": case.get("subject", "Support request"),
                        "text": step["text"],
                        "attachments": step.get("attachments") or [],
                    },
                )
            resp.raise_for_status()
            case_id = resp.json()["data"]["id"]
            await self._run_jobs(0)
        assert case_id is not None, "a case needs at least one customer message"
        return case_id

    async def _run_jobs(self, advance_hours: float) -> None:
        resp = await self.client.post("/dev/run-jobs", json={"advance_hours": advance_hours})
        resp.raise_for_status()


def judge(case: dict[str, Any], detail: dict[str, Any]) -> Outcome:
    expect = case["expect"]
    problems: list[str] = []
    wanted = expect["state"] if isinstance(expect["state"], list) else [expect["state"]]
    if detail["state"] not in wanted:
        problems.append(f"state={detail['state']} (want {' or '.join(wanted)})")
    provided = {r["key"] for r in detail["requirements"] if r["status"] == "provided"}
    missing = [k for k in expect.get("provided", []) if k not in provided]
    if missing:
        problems.append(f"not provided: {', '.join(missing)}")
    escalated = bool((detail.get("escalation") or {}).get("reason"))
    if escalated != expect.get("escalated", False):
        problems.append(
            f"escalation.reason {'set' if escalated else 'missing'} "
            f"(want {'set' if expect.get('escalated') else 'unset'})"
        )
    outbound = [m for m in detail["messages"] if m["direction"] == "outbound"]
    last = outbound[-1]["body_text"].lower() if outbound else ""
    for needle in expect.get("reply_contains", []):
        if str(needle).lower() not in last:
            problems.append(f"reply lacks {needle!r}" + ("" if outbound else " (no reply sent)"))
    return Outcome(name=case["name"], passed=not problems, problems=problems, state=detail["state"])


def print_report(outcomes: list[Outcome], runner: str) -> float:
    width = max(len(o.name) for o in outcomes)
    print(f"\nAgent eval: {runner} runner, {len(outcomes)} cases\n")
    print(f"{'case':<{width}}  result  state              detail")
    for o in outcomes:
        mark = "PASS" if o.passed else "FAIL"
        print(f"{o.name:<{width}}  {mark}    {o.state:<18} {'; '.join(o.problems)}")
    passed = sum(o.passed for o in outcomes)
    rate = passed / len(outcomes)
    total = sum(o.seconds for o in outcomes)
    print(f"\n{passed}/{len(outcomes)} passed ({rate:.0%}) in {total:.0f}s")
    return rate


async def main(args: argparse.Namespace) -> int:
    policy, cases = load_cases(CASES_FILE, args.only)
    if not cases:
        print(f"no case name contains {args.only!r}", file=sys.stderr)
        return 2
    settings = Settings(
        env="dev", scheduler_enabled=False, agent_runner="openai" if args.live else "scripted"
    )
    if args.live and not settings.openai_api_key:
        print("OPENAI_API_KEY is not set; cannot run --live", file=sys.stderr)
        return 2
    runner, assessor = build_agent(settings)
    # Real "now" so delivery windows line up with the message timestamps a live model sees.
    clock = FakeClock(datetime.now(UTC).replace(microsecond=0))
    services = build_services(
        settings,
        clock=clock,
        channels=ChannelRegistry.all_fake(),
        notifier=FakeNotifier(),
        runner=runner,
        assessor=assessor,
    )
    app = create_app(services)
    app.state.services = services
    outcomes: list[Outcome] = []
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://eval") as client:
            harness = Harness(client, services, clock)
            for case in cases:
                outcome = await harness.run_case(case, policy)
                outcomes.append(outcome)
                print(f"{'PASS' if outcome.passed else 'FAIL'}  {outcome.name}", flush=True)
    finally:
        await services.engine.dispose()
    rate = print_report(outcomes, services.runner.name)
    if rate < args.min_pass:
        print(f"below the required pass rate of {args.min_pass:.0%}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--live", action="store_true", help="use the real OpenAI runner")
    parser.add_argument(
        "--min-pass",
        type=float,
        default=DEFAULT_MIN_PASS,
        help=f"exit 1 below this pass rate (default {DEFAULT_MIN_PASS})",
    )
    parser.add_argument("--only", help="run only cases whose name contains this")
    sys.exit(asyncio.run(main(parser.parse_args())))
