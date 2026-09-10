import json

import httpx
import pytest

from app.llm.base import LLMContextOverflow, LLMInvalidOutput, LLMUnavailable
from app.llm.context import ContextBuilder
from app.llm.ollama import OllamaClient, wants_think_flag
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


@pytest.fixture
def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def good_answer(ctx):
    return {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [{
            "target": "t2", "confidence": 0.9, "item": None, "item_candidates": [],
            "fields": {k: {"status": "not_mentioned"} for k in ctx.field_keys("t2")},
            "content": None, "search_query": None,
        }],
        "notes": "",
    }


def make(handler, model="qwen3:8b"):
    return OllamaClient("http://ollama.test", model, transport=httpx.MockTransport(handler))


def test_wants_think_flag():
    assert wants_think_flag("qwen3:8b") and wants_think_flag("deepseek-r1:7b")
    assert not wants_think_flag("qwen2.5:7b-instruct") and not wants_think_flag("gemma3:4b")
    assert not wants_think_flag("qwen3-coder:30b")


async def test_interpret_happy_path(ctx):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"role": "assistant",
                                                     "content": json.dumps(good_answer(ctx))}})

    async with make(handler) as c:
        interp, trace = await c.interpret("купи молоко", ctx, build_schema(ctx))
    assert seen["path"] == "/api/chat"
    b = seen["body"]
    assert b["model"] == "qwen3:8b" and b["stream"] is False and b["think"] is False
    assert b["options"] == {"temperature": 0.0, "num_ctx": 16384}
    assert b["format"]["properties"]["candidates"]["maxItems"] == 3
    assert b["messages"][0]["role"] == "system" and "купи молоко" in b["messages"][1]["content"]
    assert interp.best.target == "t2"
    assert trace.attempts == 1 and trace.model == "qwen3:8b" and trace.duration_ms >= 0
    assert json.loads(trace.raw_response)["intent"]["value"] == "create"


async def test_no_think_flag_for_other_models(ctx):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"content": json.dumps(good_answer(ctx))}})

    async with make(handler, model="gemma3:4b") as c:
        await c.interpret("x", ctx, build_schema(ctx))
    assert "think" not in seen["body"]


async def test_retry_once_on_invalid_then_success(ctx):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["messages"])
        if len(calls) == 1:
            return httpx.Response(200, json={"message": {"content": '{"intent": "bad"}'}})
        return httpx.Response(200, json={"message": {"content": json.dumps(good_answer(ctx))}})

    async with make(handler) as c:
        interp, trace = await c.interpret("x", ctx, build_schema(ctx))
    assert trace.attempts == 2 and len(calls) == 2
    assert len(calls[1]) == 3
    assert calls[1][0]["role"] == "system" and calls[1][1]["role"] == "user"
    assert calls[1][2]["role"] == "user" and "не прошёл проверку" in calls[1][2]["content"]
    assert trace.messages == calls[1]


async def test_invalid_twice_raises(ctx):
    def handler(req):
        return httpx.Response(200, json={"message": {"content": "not json"}})

    async with make(handler) as c:
        with pytest.raises(LLMInvalidOutput) as ei:
            await c.interpret("x", ctx, build_schema(ctx))
    assert ei.value.raw == "not json"


async def test_unavailable(ctx):
    def boom(req):
        raise httpx.ConnectError("refused")

    async with make(boom) as c:
        with pytest.raises(LLMUnavailable):
            await c.interpret("x", ctx, build_schema(ctx))

    def err(req):
        return httpx.Response(500, text="boom")

    async with make(err) as c:
        with pytest.raises(LLMUnavailable):
            await c.interpret("x", ctx, build_schema(ctx))


async def test_models(ctx):
    def handler(req):
        assert req.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "gemma3:4b"}]})

    async with make(handler) as c:
        assert await c.models() == ["qwen3:8b", "gemma3:4b"]


async def test_models_skips_entries_without_name(ctx):
    def handler(req):
        return httpx.Response(
            200, json={"models": [{"name": "qwen3:8b"}, {"digest": "abc"}, {"name": "gemma3:4b"}]}
        )

    async with make(handler) as c:
        assert await c.models() == ["qwen3:8b", "gemma3:4b"]


async def test_context_overflow_raises(ctx):
    def handler(req):
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(good_answer(ctx))},
                "prompt_eval_count": 16300,
            },
        )

    async with make(handler) as c:
        with pytest.raises(LLMContextOverflow) as ei:
            await c.interpret("x", ctx, build_schema(ctx))
    assert ei.value.prompt_tokens == 16300 and ei.value.num_ctx == 16384


async def test_done_reason_length_triggers_retry_then_success(ctx):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["messages"])
        if len(calls) == 1:
            return httpx.Response(
                200, json={"message": {"content": '{"intent"'}, "done_reason": "length"}
            )
        return httpx.Response(
            200, json={"message": {"content": json.dumps(good_answer(ctx))}, "done_reason": "stop"}
        )

    async with make(handler) as c:
        interp, trace = await c.interpret("x", ctx, build_schema(ctx))
    assert trace.attempts == 2 and len(calls) == 2
    assert len(calls[1]) == 3
    assert "output truncated (num_predict)" in calls[1][2]["content"]


async def test_trace_carries_token_counts(ctx):
    def handler(req):
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(good_answer(ctx))},
                "prompt_eval_count": 500,
                "eval_count": 120,
                "done_reason": "stop",
            },
        )

    async with make(handler) as c:
        interp, trace = await c.interpret("x", ctx, build_schema(ctx))
    assert trace.prompt_tokens == 500 and trace.output_tokens == 120
    assert trace.done_reason == "stop" and trace.truncated is False


async def test_non_json_response_raises_unavailable(ctx):
    def handler(req):
        return httpx.Response(200, text="not-json-body")

    async with make(handler) as c:
        with pytest.raises(LLMUnavailable):
            await c.interpret("x", ctx, build_schema(ctx))


async def test_null_content_becomes_empty_raw(ctx):
    def handler(req):
        return httpx.Response(200, json={"message": {"content": None}})

    async with make(handler) as c:
        with pytest.raises(LLMInvalidOutput) as ei:
            await c.interpret("x", ctx, build_schema(ctx))
    assert ei.value.raw == ""
