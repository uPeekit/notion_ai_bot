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


def test_merge_puts_images_before_the_sources():
    from app.llm.research import merge

    text = "## Тории\nтекст\n\n## Источники\n- [a](https://a)"
    merged = merge(text, ["![x](https://i/x.jpg)"])
    assert merged.index("## Изображения") < merged.index("## Источники")
    assert merge("", ["![x](https://i/x.jpg)"]).startswith("## Изображения")
    assert merge(text, []) == text


class FakeSearch:
    def __init__(self, commons=(), og=None):
        self.phrases: list[str] = []
        self._commons, self._og = list(commons), og or {}

    async def commons(self, phrase, limit=6):
        self.phrases.append(phrase)
        return list(self._commons)

    async def og_image(self, url):
        return self._og.get(url)


def by_system(text_answer: str, phrases: str = "torii gate\noak bench"):
    def handler(req):
        body = json.loads(req.content)
        answer = phrases if "Wikimedia" in body["system"] else text_answer
        return message([{"type": "text", "text": answer}])
    return handler


def sdk(handler):
    return anthropic.AsyncAnthropic(api_key=KEY, max_retries=0, http_client=httpx2.AsyncClient(
        transport=httpx2.MockTransport(handler)))


TEXT = "## Тории\nтекст\n\n## Источники\n- [Вики](https://ru.wikipedia.org/wiki/Torii)"


async def test_pictures_come_from_commons_and_the_cited_pages_and_are_checked():
    search = FakeSearch(
        commons=[("https://up.example/a.jpg", "Itsukushima Gate"),
                 ("https://dead.example/b.jpg", "Dead")],
        og={"https://ru.wikipedia.org/wiki/Torii": ("https://up.example/og.jpg", "Тории")})

    async def is_image(url):
        return "dead." not in url

    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(by_system(TEXT)),
                      is_image=is_image, search=search)
    out = await r.research("тории с картинками", "тории", "text_and_images")
    assert search.phrases == ["torii gate", "oak bench"]
    assert "![Itsukushima Gate](https://up.example/a.jpg)" in out
    assert "![Тории](https://up.example/og.jpg)" in out
    assert "dead.example" not in out
    assert out.index("## Изображения") < out.index("## Источники")


async def test_images_only_skips_the_text_search_and_text_only_skips_pictures():
    calls = []

    def handler(req):
        calls.append(json.loads(req.content)["system"])
        return by_system(TEXT)(req)

    search = FakeSearch(commons=[("https://up.example/a.jpg", "A")])
    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(handler), search=search)
    only = await r.research("картинки тории", "тории", "images")
    assert only.startswith("## Изображения") and "## Тории" not in only
    assert all("Wikimedia" in s for s in calls)  # no text research ran
    calls.clear()
    text = await r.research("про тории", "тории", "text")
    assert "## Изображения" not in text and not any("Wikimedia" in s for s in calls)


async def test_no_usable_images_for_an_images_only_request_is_an_error():
    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(by_system(TEXT)),
                      search=FakeSearch())
    with pytest.raises(ResearchError, match="no images"):
        await r.research("картинки", "q", "images")


async def test_a_question_line_becomes_a_research_question():
    from app.llm.research import ResearchQuestion

    r = researcher(lambda req: message([{"type": "text", "text": "ВОПРОС: Искать или нет?"}]))
    with pytest.raises(ResearchQuestion) as e:
        await r.research("не найди картинки", "картинки")
    assert e.value.question == "Искать или нет?"


def by_role(text_answer: str, keep: list[int], phrases: str = "helsinki"):
    """Text research, Commons phrases and the relevance check, told apart by their prompts."""
    def handler(req):
        system = json.loads(req.content)["system"]
        if "отбираешь картинки" in system:
            answer = json.dumps({"keep": keep})
        elif "Придумай" in system:
            answer = phrases
        else:
            answer = text_answer
        return message([{"type": "text", "text": answer}])
    return handler


async def test_off_topic_pictures_are_dropped_by_the_relevance_check():
    hits = [("https://up.example/letter.jpg", "Letter signed Sara, London (page 1)"),
            ("https://up.example/harbour.jpg", "Helsinki harbour at dusk")]
    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(by_role(TEXT, keep=[2])),
                      search=FakeSearch(commons=hits))
    out = await r.research("Хельсинки с ребёнком", "хельсинки", "images")
    assert "harbour.jpg" in out and "letter.jpg" not in out


async def test_pages_of_one_scanned_document_collapse_into_one():
    pages = [(f"https://up.example/p{n}.jpg", f"Letter signed Sara, London, 1923 (page {n})")
             for n in range(1, 7)]
    seen = []

    def handler(req):
        body = json.loads(req.content)
        if "отбираешь картинки" in body["system"]:
            seen.append(body["messages"][0]["content"])
        return by_role(TEXT, keep=[1])(req)

    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(handler),
                      search=FakeSearch(commons=pages))
    out = await r.research("письма", "letters", "images")
    assert out.count("![") == 1
    assert seen[0].count("Letter signed Sara") == 1  # the check saw the document once


async def test_a_failed_relevance_check_keeps_the_pictures():
    def handler(req):
        if "отбираешь картинки" in json.loads(req.content)["system"]:
            return httpx2.Response(529, json={
                "type": "error", "error": {"type": "overloaded_error", "message": "busy"}})
        return by_role(TEXT, keep=[])(req)

    r = WebResearcher(KEY, "claude-haiku-4-5", client=sdk(handler),
                      search=FakeSearch(commons=[("https://up.example/a.jpg", "A")]))
    assert "a.jpg" in await r.research("x", "y", "images")


async def test_a_phrase_that_finds_nothing_is_retried_shorter():
    class Picky(FakeSearch):
        async def commons(self, phrase, limit=6):
            self.phrases.append(phrase)
            return [("https://up.example/f.jpg", "Ferry")] if phrase == "Viking Line" else []

    search = Picky()
    r = WebResearcher(KEY, "claude-haiku-4-5", search=search,
                      client=sdk(by_role(TEXT, keep=[1], phrases="Viking Line family trip")))
    out = await r.research("паром", "паром", "images")
    assert search.phrases == ["Viking Line family trip", "Viking Line family", "Viking Line"]
    assert "f.jpg" in out
