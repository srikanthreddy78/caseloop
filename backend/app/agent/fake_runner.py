"""Deterministic agent used by tests and `make demo`. Exercises the real tools without an LLM."""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.schemas import CaseAssessment, ExtractedValue, RequirementSpec
from app.agent.tools import CaseTools, TurnContext
from app.models import Business, Case, Direction

_ORDER_RE = re.compile(
    r"(?:order(?:\s+(?:number|num|no\.?|id|is|was))*|ord|#)\s*[#:]?\s*([A-Z]{0,4}-?\d{3,}[A-Z0-9-]*)",
    re.IGNORECASE,
)

_CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("damaged", ("damaged", "broken", "cracked", "smashed", "dented", "shattered")),
    (
        "missing",
        ("never arrived", "missing", "not delivered", "lost", "hasn't arrived", "didn't arrive"),
    ),
    ("refund", ("refund", "money back", "charge back")),
    ("return", ("return", "send it back", "exchange")),
    ("billing", ("charged", "invoice", "billing", "double charged", "subscription")),
]

_RESOLUTION_BY_CATEGORY = {
    "damaged": "replacement_sent",
    "missing": "replacement_sent",
    "refund": "refund_issued",
    "return": "return_approved",
    "billing": "credit_issued",
    "question": "information_provided",
    "other": "information_provided",
}


class ScriptedAssessor:
    name = "scripted"

    async def assess(
        self, business: Business, case: Case, snapshot: dict[str, Any]
    ) -> CaseAssessment | None:
        inbound = [m for m in case.messages if m.direction == Direction.INBOUND]
        text = "\n".join(m.body_text for m in inbound)
        lowered = text.lower()
        category = "question"
        for name, words in _CATEGORY_RULES:
            if any(w in lowered for w in words):
                category = name
                break
        required = [
            RequirementSpec(
                key="order_number",
                label="Order number",
                why_needed="Needed to find the purchase in the order system.",
            )
        ]
        if category == "damaged":
            required.append(
                RequirementSpec(
                    key="photos",
                    label="Photos of the damage",
                    why_needed="Required by policy before a replacement can be approved.",
                )
            )
        extracted: list[ExtractedValue] = []
        match = _ORDER_RE.search(text)
        if match:
            extracted.append(ExtractedValue(key="order_number", value=match.group(1).upper()))
        if any(m.attachments for m in inbound):
            names = [a.get("filename", "file") for m in inbound for a in m.attachments]
            extracted.append(ExtractedValue(key="photos", value=", ".join(names)))
        angry = any(
            w in lowered for w in ("unacceptable", "furious", "ridiculous", "lawyer", "worst")
        )
        subject = case.subject or (inbound[0].body_text[:60] if inbound else "Support request")
        return CaseAssessment(
            category=category,
            subject=subject,
            summary=f"Customer reports a {category} issue. "
            + (inbound[0].body_text[:160] if inbound else ""),
            sentiment="angry" if angry else "frustrated",
            urgency="high" if angry else "normal",
            remedy_requested="replacement" if category in ("damaged", "missing") else "unspecified",
            required=required,
            extracted=extracted,
        )


class ScriptedAgentRunner:
    name = "scripted"

    async def run_turn(self, ctx: TurnContext, tools: CaseTools, system: str) -> dict[str, Any]:
        snapshot = json.loads(await tools.dispatch("get_case", {}))
        missing = [r for r in snapshot["requirements"] if r["status"] == "missing"]
        labels = ", ".join(r["label"].lower() for r in missing)
        name = snapshot["customer"].get("name") or "there"
        category = snapshot.get("category") or "support"
        lowered = " ".join(
            m["text"].lower() for m in snapshot["conversation"] if m["direction"] == "inbound"
        )
        if any(w in lowered for w in ("lawyer", "legal action", "sue you")):
            await tools.dispatch(
                "escalate",
                {
                    "reason": "legal_threat",
                    "summary_for_human": "Customer mentioned legal action. Needs a human reply.",
                },
            )
            return {"runner": self.name}
        if ctx.trigger == "followup_due":
            text = (
                f"Hi {name}, just checking in. To move your {category} request forward we still need: "
                f"{labels}. Reply here whenever you can and I will take it from there."
            )
            await tools.dispatch("send_message", {"text": text})
            await tools.dispatch("schedule_followup", {"hours": 24, "reason": "nudge sent"})
            return {"runner": self.name}
        if missing:
            text = (
                f"Hi {name}, thanks for reaching out and sorry about the trouble. To sort this out I need: "
                f"{labels}. Reply with that and I will get it moving right away."
            )
            await tools.dispatch("send_message", {"text": text})
            await tools.dispatch(
                "schedule_followup", {"hours": 24, "reason": "asked for missing items"}
            )
            return {"runner": self.name}
        resolution = _RESOLUTION_BY_CATEGORY.get(category, "information_provided")
        text = (
            f"Thanks {name}, I have everything I need. I've logged this as {resolution.replace('_', ' ')} "
            "and you'll get a confirmation shortly. Reply here if anything else comes up."
        )
        await tools.dispatch("send_message", {"text": text})
        await tools.dispatch(
            "close_case",
            {"resolution_type": resolution, "notes": f"Scripted resolution for {category} case."},
        )
        return {"runner": self.name}
