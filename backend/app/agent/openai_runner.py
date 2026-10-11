"""OpenAI provider: Responses API with strict function tools (turns) and structured outputs (assessment)."""

from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Any, cast

from openai import AsyncOpenAI

from app.agent.prompts import assessment_prompt
from app.agent.schemas import CaseAssessment
from app.agent.tools import CaseTools, TurnContext, scrub_pii
from app.models import Business, Case, Direction

log = logging.getLogger(__name__)

MAX_ROUNDS = 8
TURN_REASONING = "low"
ASSESS_REASONING = "low"


def _function_tools(specs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": s["name"],
            "description": s["description"],
            "parameters": s["parameters"],
            "strict": True,
        }
        for s in specs
    ]


class OpenAIAgentRunner:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, model: str) -> None:
        self.client = client
        self.model = model

    async def run_turn(self, ctx: TurnContext, tools: CaseTools, system: str) -> dict[str, Any]:
        opening = (
            f"Trigger: {ctx.trigger}. Case channel: {ctx.case.channel}. "
            "Start by calling get_case, then act. Remember to finish with close_case, escalate, or "
            "send_message followed by schedule_followup."
        )
        input_list: list[Any] = [{"role": "user", "content": opening}]
        usage: dict[str, int] = {"input_tokens": 0, "output_tokens": 0}
        response_ids: list[str] = []
        for _round in range(MAX_ROUNDS):
            response = await self.client.responses.create(
                model=self.model,
                instructions=system,
                input=input_list,
                tools=cast(Any, _function_tools(tools.specs())),
                reasoning=cast(Any, {"effort": TURN_REASONING}),
                store=False,
                max_output_tokens=2500,
            )
            response_ids.append(response.id)
            if response.usage is not None:
                usage["input_tokens"] += response.usage.input_tokens
                usage["output_tokens"] += response.usage.output_tokens
            input_list += response.output
            calls = [item for item in response.output if item.type == "function_call"]
            if not calls:
                break
            for call in calls:
                try:
                    args = json.loads(call.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = await tools.dispatch(call.name, args)
                input_list.append(
                    {"type": "function_call_output", "call_id": call.call_id, "output": result}
                )
            if ctx.terminal_tool is not None:
                break
        return {"runner": self.name, "model": self.model, "usage": usage, "responses": response_ids}


class OpenAIAssessor:
    name = "openai"

    def __init__(self, client: AsyncOpenAI, model: str, data_dir: Path | None = None) -> None:
        self.client = client
        self.model = model
        self.data_dir = data_dir

    async def assess(
        self, business: Business, case: Case, snapshot: dict[str, Any]
    ) -> CaseAssessment | None:
        content: list[dict[str, Any]] = [
            {"type": "input_text", "text": _conversation_text(case, snapshot)}
        ]
        content.extend(self._image_parts(case))
        response = await self.client.responses.parse(
            model=self.model,
            instructions=assessment_prompt(business),
            input=cast(Any, [{"role": "user", "content": content}]),
            text_format=CaseAssessment,
            reasoning=cast(Any, {"effort": ASSESS_REASONING}),
            max_output_tokens=2000,
        )
        return response.output_parsed

    def _image_parts(self, case: Case, limit: int = 4) -> list[dict[str, Any]]:
        parts: list[dict[str, Any]] = []
        if self.data_dir is None:
            return parts
        last_inbound = next(
            (m for m in reversed(case.messages) if m.direction == Direction.INBOUND), None
        )
        if last_inbound is None:
            return parts
        for att in last_inbound.attachments or []:
            ctype = str(att.get("content_type", ""))
            key = att.get("storage_key")
            if not ctype.startswith("image/") or not key:
                continue
            path = self.data_dir / key
            if not path.is_file() or path.stat().st_size > 8_000_000:
                continue
            data = base64.b64encode(path.read_bytes()).decode()
            parts.append(
                {"type": "input_image", "image_url": f"data:{ctype};base64,{data}", "detail": "low"}
            )
            if len(parts) >= limit:
                break
        return parts


def _conversation_text(case: Case, snapshot: dict[str, Any]) -> str:
    lines = [f"Channel: {case.channel}", f"Subject: {case.subject or '(none)'}"]
    existing = snapshot.get("requirements") or []
    if existing:
        lines.append("Requirements already tracked: " + json.dumps(existing, default=str))
    lines.append("Conversation (oldest first):")
    for m in case.messages[-30:]:
        if m.direction == Direction.INTERNAL:
            continue
        text = scrub_pii(m.body_text) if m.direction == Direction.INBOUND else m.body_text
        names = ", ".join(str(a.get("filename")) for a in (m.attachments or []))
        suffix = f" [attachments: {names}]" if names else ""
        lines.append(f"- {m.sender}: {text}{suffix}")
    return "\n".join(lines)
