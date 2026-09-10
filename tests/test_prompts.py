from app.llm.context import ContextBuilder
from app.llm.prompts import SYSTEM_PROMPT, build_messages, retry_message
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def test_messages_structure():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    msgs = build_messages("  купи молоко ", ctx)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == SYSTEM_PROMPT
    assert msgs[1]["content"].endswith("«купи молоко»")
    assert '"key":"t2"' in msgs[1]["content"] and "2026-09-09T18:00+03:00" in msgs[1]["content"]


def test_system_prompt_mentions_rules():
    for needle in ("not_mentioned", "ambiguous", "explicit_null", "item_candidates", "search_query",
                   "только ключи", "YYYY-MM-DD"):
        assert needle in SYSTEM_PROMPT


def test_retry_message_contains_error():
    assert "поле X" in retry_message("поле X")
