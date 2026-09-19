import json
from dataclasses import replace

import anthropic
import httpx2
import pytest

from app.llm.base import LLMInvalidOutput, LLMUnavailable
from app.llm.claude import ClaudeClient, check, flat_schema, to_interpretation
from app.llm.context import ContextBuilder
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

KEY = "sk-ant-test-key"


@pytest.fixture
def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def reply(text: str, stop_reason: str = "end_turn") -> httpx2.Response:
    return httpx2.Response(200, json={
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": text}], "stop_reason": stop_reason,
        "stop_sequence": None, "usage": {"input_tokens": 3800, "output_tokens": 300},
    })


def error(status: int, message: str) -> httpx2.Response:
    return httpx2.Response(status, json={
        "type": "error", "error": {"type": "some_error", "message": message}})


def make(handler) -> ClaudeClient:
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeClient(KEY, "claude-haiku-4-5", client=sdk)


def flat(ctx, **over):
    """good_answer(ctx) in the flat shape Claude answers in."""
    answer = {
        "notes": "", "intent": {"value": "create", "confidence": 0.95},
        "candidates": [{
            "target_name": "x", "target": "t2", "confidence": 0.9, "item": "",
            "item_candidates": [], "item_text": "", "fields": [], "content": "",
            "search_query": "",
        }],
    }
    answer["candidates"][0].update(over)
    return answer


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def test_flat_schema_has_no_unions_and_closes_every_object(ctx):
    nodes = list(_walk(flat_schema(ctx)))
    assert not any("anyOf" in n or isinstance(n.get("type"), list) for n in nodes)
    assert all(n["additionalProperties"] is False for n in nodes if n.get("type") == "object")


def test_flat_answer_converts_to_one_that_fits_the_full_schema(ctx):
    tk = "t2"
    fks = ctx.field_keys(tk)
    title = next(fk for fk in fks if ctx.ref(fk).field_type == "title")
    answer = to_interpretation(flat(ctx, fields=[
        {"key": title, "status": "value", "value_json": '"Молоко"', "confidence": 0.9,
         "source_text": "молоко"},
    ]), ctx)
    assert check(answer, build_schema(ctx)) == ""
    c = answer["candidates"][0]
    assert c["item"] is None and c["content"] is None  # "" came back as null
    assert c["fields"][title]["value"] == "Молоко"
    assert all(c["fields"][fk] == {"status": "not_mentioned"} for fk in fks if fk != title)
    assert c["target_name"] == ctx.target_labels[tk]


def test_a_converted_answer_breaking_the_full_schema_is_reported(ctx):
    answer = to_interpretation(flat(ctx, item="t2.i999"), ctx)
    assert "item" in check(answer, build_schema(ctx))


def test_value_json_that_is_not_json_is_a_value_error(ctx):
    fk = ctx.field_keys("t2")[0]
    with pytest.raises(ValueError, match="not JSON"):
        to_interpretation(flat(ctx, fields=[
            {"key": fk, "status": "value", "value_json": "Молоко", "confidence": 1,
             "source_text": ""}]), ctx)


async def test_interpret_sends_system_schema_and_returns_the_answer(ctx):
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["key"] = req.headers.get("x-api-key")
        seen["body"] = json.loads(req.content)
        return reply(json.dumps(flat(ctx)))

    async with make(handler) as c:
        interp, trace = await c.interpret("купи молоко", ctx, build_schema(ctx))
    b = seen["body"]
    assert seen["path"] == "/v1/messages" and seen["key"] == KEY
    assert b["model"] == "claude-haiku-4-5"
    assert b["system"].startswith("Ты — модуль интерпретации")
    assert [m["role"] for m in b["messages"]] == ["user"]
    assert "купи молоко" in b["messages"][0]["content"]
    assert b["output_config"]["format"] == {"type": "json_schema", "schema": flat_schema(ctx)}
    assert interp.best.target == "t2"
    assert trace.model == "claude-haiku-4-5" and trace.attempts == 1
    assert (trace.prompt_tokens, trace.output_tokens) == (3800, 300)


async def test_an_answer_that_fails_validation_is_retried_once_with_the_error(ctx):
    bad = flat(ctx)
    bad["intent"]["confidence"] = 1.7  # the bound structured outputs could not enforce
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return reply(json.dumps(bad if len(bodies) == 1 else flat(ctx)))

    async with make(handler) as c:
        interp, trace = await c.interpret("x", ctx, build_schema(ctx))
    assert trace.attempts == 2 and interp.intent.confidence == 0.95
    retry = bodies[1]["messages"]
    assert [m["role"] for m in retry] == ["user", "assistant", "user"]
    assert "intent/confidence" in retry[2]["content"]


async def test_two_invalid_answers_raise_invalid_output(ctx):
    async with make(lambda req: reply("{}")) as c:
        with pytest.raises(LLMInvalidOutput):
            await c.interpret("x", ctx, build_schema(ctx))


async def test_refusal_is_invalid_output_without_a_retry(ctx):
    calls = []

    def handler(req):
        calls.append(1)
        return reply("", stop_reason="refusal")

    async with make(handler) as c:
        with pytest.raises(LLMInvalidOutput):
            await c.interpret("x", ctx, build_schema(ctx))
    assert len(calls) == 1


@pytest.mark.parametrize("status,message", [
    (401, "invalid x-api-key"),
    (429, "rate limited"),
    (400, "Your credit balance is too low to access the Anthropic API."),
    (529, "Overloaded"),
])
async def test_api_errors_become_unavailable_and_never_carry_the_key(ctx, status, message):
    async with make(lambda req: error(status, message)) as c:
        with pytest.raises(LLMUnavailable) as e:
            await c.interpret("x", ctx, build_schema(ctx))
    assert str(status) in str(e.value) and KEY not in str(e.value)


async def test_no_connection_is_unavailable(ctx):
    def handler(req):
        raise httpx2.ConnectError("offline")

    async with make(handler) as c:
        with pytest.raises(LLMUnavailable, match="unreachable"):
            await c.interpret("x", ctx, build_schema(ctx))


async def test_models_checks_the_configured_model():
    def handler(req):
        assert req.url.path == "/v1/models/claude-haiku-4-5"
        return httpx2.Response(200, json={
            "type": "model", "id": "claude-haiku-4-5-20251001",
            "display_name": "Claude Haiku 4.5", "created_at": "2025-10-01T00:00:00Z"})

    async with make(handler) as c:
        assert "claude-haiku-4-5" in await c.models()


async def test_models_with_a_bad_key_is_unavailable():
    async with make(lambda req: error(401, "invalid x-api-key")) as c:
        with pytest.raises(LLMUnavailable, match="401"):
            await c.models()


async def test_claude_gets_the_cloud_view_of_the_context():
    snap = sample_snapshot()
    snap = replace(snap, targets=[replace(t, local_only=t.id == "ds-buy") for t in snap.targets])
    ctx = ContextBuilder().build(snap, now=SAMPLE_NOW)
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return reply(json.dumps(flat(ctx)))

    async with make(handler) as c:
        await c.interpret("x", ctx, build_schema(ctx))
    assert ctx.json(cloud=True) in seen["body"]["messages"][0]["content"]
    assert ctx.json() not in seen["body"]["messages"][0]["content"]


def test_web_query_survives_the_flat_round_trip():
    ctx = ContextBuilder(web_research=True).build(sample_snapshot(), now=SAMPLE_NOW)
    answer = to_interpretation(flat(ctx, web_query="рецепт борща"), ctx)
    assert answer["candidates"][0]["web_query"] == "рецепт борща"
    assert check(answer, build_schema(ctx)) == ""
    assert to_interpretation(flat(ctx, web_query=""), ctx)["candidates"][0]["web_query"] is None


def test_a_single_option_for_a_list_field_is_wrapped_in_a_list(ctx):
    project = ctx.field_key("ds-todo", "project")  # relation
    tk = ctx.target_key("ds-todo")
    option = ctx.option_keys(project)[0]
    answer = to_interpretation(flat(ctx, target=tk, fields=[
        {"key": project, "status": "value", "value_json": json.dumps(option),
         "confidence": 0.9, "source_text": "по дому"}]), ctx)
    assert answer["candidates"][0]["fields"][project]["value"] == [option]
    assert check(answer, build_schema(ctx)) == ""


def test_schema_errors_name_the_field_not_just_the_candidate(ctx):
    tk = ctx.target_key("ds-todo")
    due = ctx.field_key("ds-todo", "due")
    answer = to_interpretation(flat(ctx, target=tk, fields=[
        {"key": due, "status": "value", "value_json": '"завтра"', "confidence": 0.9,
         "source_text": ""}]), ctx)
    error = check(answer, build_schema(ctx))
    assert f"candidates/0/fields/{due}" in error
    assert "not valid under any" not in error.split(":")[0]
