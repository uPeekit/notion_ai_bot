from app.llm.context import ContextBuilder
from app.llm.prompts import (
    NOTES_FIRST,
    NOTES_LAST,
    SYSTEM_PROMPT,
    TARGET_NAME_RULE,
    build_messages,
    retry_message,
    system_prompt,
)
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def test_messages_structure():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    msgs = build_messages("  купи молоко ", ctx)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == system_prompt(ctx)
    assert msgs[1]["content"].endswith("«купи молоко»")
    assert '"key":"t2"' in msgs[1]["content"] and "2026-09-09T18:00+03:00" in msgs[1]["content"]


def test_default_prompt_reasons_first_and_names_targets():
    prompt = system_prompt(ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW))
    assert NOTES_FIRST in prompt and TARGET_NAME_RULE in prompt and NOTES_LAST not in prompt


def test_baseline_prompt_is_unchanged_with_both_switches_off():
    ctx = ContextBuilder(reasoning_first=False, name_targets=False).build(
        sample_snapshot(), now=SAMPLE_NOW)
    assert system_prompt(ctx) == SYSTEM_PROMPT


def test_system_prompt_mentions_rules():
    for needle in ("not_mentioned", "ambiguous", "explicit_null", "item_candidates", "search_query",
                   "только ключи", "YYYY-MM-DD", "никогда через ambiguous",
                   "независимо от того, обязательное",
                   "блок calendar", "понедельник следующей недели", "Примеры"):
        assert needle in SYSTEM_PROMPT


def test_retry_message_contains_error():
    assert "поле X" in retry_message("поле X")


def test_prompt_lets_described_options_be_chosen_by_meaning_and_follows_the_note():
    assert "option_descriptions" in SYSTEM_PROMPT and "workspace_note" in SYSTEM_PROMPT
    assert "Варианты без описания не угадывай" in SYSTEM_PROMPT
