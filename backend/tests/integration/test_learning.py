from __future__ import annotations

from app.agent.prompts import system_prompt
from app.learning import learning_block
from app.models import Business, Lesson
from httpx import AsyncClient

from tests.integration.test_email_flow import run_jobs, simulate


async def test_human_decisions_become_lessons_the_agent_reads(auth_client: AsyncClient) -> None:
    case = await simulate(auth_client, "My order 4141 arrived dented, want a refund.")
    await run_jobs(auth_client)
    reply = await auth_client.post(
        f"/v1/cases/{case['id']}/messages", json={"text": "So sorry, we will sort this out today."}
    )
    assert reply.status_code == 201
    done = await auth_client.post(
        f"/v1/cases/{case['id']}/resolve",
        json={
            "resolution_type": "replacement_sent",
            "notes": "Dented bases always get a replacement, no photo needed.",
        },
    )
    assert done.status_code == 200
    lessons = (await auth_client.get("/v1/lessons")).json()["data"]
    kinds = {lesson["kind"] for lesson in lessons}
    assert {"human_reply", "human_resolution"} <= kinds
    assert any("Dented bases always get a replacement" in lesson["text"] for lesson in lessons)

    gone = await auth_client.delete(f"/v1/lessons/{lessons[0]['id']}")
    assert gone.status_code == 204
    assert len((await auth_client.get("/v1/lessons")).json()["data"]) == len(lessons) - 1


def test_learning_block_lands_in_the_prompt() -> None:
    business = Business(name="Acme", context_text="policy")
    lesson = Lesson(kind="human_resolution", text="Dented bases get a replacement.")
    block = learning_block(
        [lesson],
        [
            {
                "subject": "Cracked shade",
                "summary": "",
                "resolution": "refund_issued",
                "notes": "under $150",
                "by": "human",
            }
        ],
    )
    prompt = system_prompt(business, block)
    assert "Dented bases get a replacement." in prompt
    assert "Cracked shade -> refund issued (decided by a person): under $150" in prompt
    assert "What this business has taught you" in prompt
    assert system_prompt(business, "").count("taught you") == 0
