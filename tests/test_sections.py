import json

import anthropic
import httpx2

from app.llm.sections import MAX_SECTIONS, SectionPicker

KEY = "sk-ant-test-key"
HEADINGS = ["читать", "смотреть", "подкасты"]


def answer(section):
    return httpx2.Response(200, json={
        "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": json.dumps({"section": section})}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 300, "output_tokens": 8},
    })


def picker(handler) -> SectionPicker:
    sdk = anthropic.AsyncAnthropic(
        api_key=KEY, max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)))
    return SectionPicker(KEY, "claude-haiku-4-5", client=sdk)


async def test_the_chosen_heading_comes_back_as_an_index():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return answer(2)

    got = await picker(handler).pick("надо посмотреть фильм Uncharted", "Uncharted", HEADINGS)

    assert got == 1  # "смотреть", the second heading
    sent = bodies[0]["messages"][0]["content"]
    assert "Uncharted" in sent and "подкасты" in sent
    assert bodies[0]["max_tokens"] <= 64  # one number back, nothing else


async def test_a_section_outside_the_list_and_zero_are_both_no_answer():
    for section in (0, 9, -1):
        p = picker(lambda r, s=section: answer(s))
        assert await p.pick("x", "y", HEADINGS) is None


async def test_nothing_is_asked_when_there_is_no_choice_to_make():
    calls = []

    def handler(request):
        calls.append(request)
        return answer(1)

    p = picker(handler)
    assert await p.pick("x", "y", ["смотреть"]) is None
    assert await p.pick("x", "y", [f"h{i}" for i in range(MAX_SECTIONS + 1)]) is None
    assert calls == []


async def test_a_failed_call_is_not_an_error_just_no_answer():
    def handler(request):
        return httpx2.Response(500, json={"error": {"message": "boom"}})

    assert await picker(handler).pick("x", "y", HEADINGS) is None


async def test_a_reply_that_is_not_the_expected_shape_is_no_answer():
    def handler(request):
        return httpx2.Response(200, json={
            "id": "m", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "не знаю"}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 1, "output_tokens": 1}})

    assert await picker(handler).pick("x", "y", HEADINGS) is None
