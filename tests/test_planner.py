import json

import anthropic
import httpx2
import pytest

from app.conversation.plan import MAX_STEPS, PlanState, PlanStep
from app.llm.planner import PlanError, Planner

KEY = "sk-ant-test-key"


def message(payload) -> httpx2.Response:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return httpx2.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
        "stop_sequence": None, "usage": {"input_tokens": 2000, "output_tokens": 200},
    })


def planner(handler) -> Planner:
    sdk = anthropic.AsyncAnthropic(api_key=KEY, max_retries=0, http_client=httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler)))
    return Planner(KEY, "claude-haiku-4-5", client=sdk)


async def test_plan_returns_goal_and_steps_under_a_schema():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return message({"goal": "Книги в Books", "steps": ["добавь в Books Солярис", " ",
                                                           "добавь в Books Дюна"]})

    goal, steps = await planner(handler).plan("добавь книги Солярис и Дюна", "Места в Notion:")
    assert goal == "Книги в Books" and steps == ["добавь в Books Солярис", "добавь в Books Дюна"]
    body = bodies[0]
    assert body["output_config"]["format"]["schema"]["required"] == ["goal", "steps"]
    assert "Места в Notion:" in body["messages"][0]["content"]
    assert "добавь книги" in body["messages"][0]["content"]


async def test_plan_is_capped_and_an_empty_one_is_an_error():
    many = {"goal": "g", "steps": [f"шаг {i}" for i in range(MAX_STEPS + 10)]}
    _, steps = await planner(lambda req: message(many)).plan("x", "w")
    assert len(steps) == MAX_STEPS
    with pytest.raises(PlanError):
        await planner(lambda req: message({"goal": "g", "steps": []})).plan("x", "w")
    with pytest.raises(PlanError):
        await planner(lambda req: message("не JSON")).plan("x", "w")


async def test_next_reads_the_verdict_and_sends_the_history():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return message({"done": False, "summary": "", "next_step": "добавь в Books Дюна"})

    state = PlanState(goal="g", planned=["a", "b"], current="a",
                      history=[PlanStep(request="a", status="failed", outcome="Не понял")])
    verdict = await planner(handler).next(state, "w")
    assert (verdict.done, verdict.next_step) == (False, "добавь в Books Дюна")
    content = bodies[0]["messages"][0]["content"]
    assert "Не понял" in content and "failed" in content


async def test_next_without_a_next_step_or_on_an_error_is_done():
    done = await planner(lambda req: message(
        {"done": False, "summary": "", "next_step": " "})).next(
        PlanState(goal="g", planned=["a"], current="a"), "w")
    assert done.done
    failed = await planner(lambda req: httpx2.Response(529, json={
        "type": "error", "error": {"type": "overloaded_error", "message": "busy"}})).next(
        PlanState(goal="g", planned=["a"], current="a"), "w")
    assert failed.done  # stop rather than guess on


async def test_a_cut_off_plan_is_reported_as_such():
    def handler(req):
        assert json.loads(req.content)["max_tokens"] >= 8000  # room to think and to answer
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-sonnet-5",
            "content": [{"type": "text", "text": '{"goal":"g","steps":["a",'}],
            "stop_reason": "max_tokens", "stop_sequence": None,
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    with pytest.raises(PlanError, match="cut off"):
        await planner(handler).plan("x", "w")
