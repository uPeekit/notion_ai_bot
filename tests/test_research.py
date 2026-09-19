import json
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from app.llm.research import (
    MAX_FETCH_TOKENS,
    ResearchError,
    WebResearcher,
    final_text,
    research_tools,
)

KEY = "sk-ant-test-key"


def block(type_, text=""):
    return SimpleNamespace(type=type_, text=text)


def test_final_text_is_what_comes_after_the_last_tool_result():
    blocks = [block("text", "Сейчас поищу."), block("server_tool_use"),
              block("web_search_tool_result"), block("text", "Нашёл! "),
              block("text", "## Итог\n- пункт")]
    assert final_text(blocks) == "## Итог\n- пункт"  # the chatty lead-in line dropped too


def test_final_text_without_any_tool_use_is_all_the_text():
    assert final_text([block("text", "## Ответ")]) == "## Ответ"


def test_tool_versions_follow_the_model():
    haiku = {t["type"] for t in research_tools("claude-haiku-4-5", 3)}
    sonnet = {t["type"] for t in research_tools("claude-sonnet-5", 3)}
    assert haiku == {"web_search_20250305", "web_fetch_20250910"}
    assert sonnet == {"web_search_20260209", "web_fetch_20260209"}
    fetch = next(t for t in research_tools("claude-haiku-4-5", 3) if t["name"] == "web_fetch")
    assert fetch["max_uses"] == 3 and fetch["max_content_tokens"] == MAX_FETCH_TOKENS


def message(content, stop_reason="end_turn"):
    return httpx2.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 30000, "output_tokens": 900},
    })


def researcher(handler) -> WebResearcher:
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return WebResearcher(KEY, "claude-haiku-4-5", client=sdk)


async def test_research_sends_tools_and_resumes_a_paused_turn():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        if len(bodies) == 1:
            return message([{"type": "text", "text": "Ищу…"}], stop_reason="pause_turn")
        return message([{"type": "text", "text": "## Борщ\n- свёкла"}])

    r = researcher(handler)
    assert await r.research("найди рецепт борща", "рецепт борща") == "## Борщ\n- свёкла"
    first, second = bodies
    assert {t["name"] for t in first["tools"]} == {"web_search", "web_fetch"}
    assert "рецепт борща" in first["messages"][0]["content"]
    assert [m["role"] for m in second["messages"]] == ["user", "assistant"]


async def test_an_empty_answer_or_an_api_error_is_a_research_error():
    r = researcher(lambda req: message([]))
    with pytest.raises(ResearchError, match="no answer"):
        await r.research("x", "y")
    r = researcher(lambda req: httpx2.Response(429, json={
        "type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}}))
    with pytest.raises(ResearchError, match="429"):
        await r.research("x", "y")


def test_a_hash_inside_a_word_is_not_a_heading():
    assert final_text([block("text", "Язык C# 12 — ## не заголовок")]) == "## не заголовок"
    assert final_text([block("text", "Про C# и F#.")]) == "Про C# и F#."
