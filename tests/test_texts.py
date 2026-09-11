"""Every user-visible Russian string lives in app.texts. This test checks completeness
(every QType has a question template, every documented error code has a message) and that
every placeholder-bearing template can actually be filled in without leaving stray braces."""

from __future__ import annotations

from typing import get_args

from app import texts
from app.validation.policy import QType

ERROR_CODES = [
    "STT_EMPTY", "STT_FAILED", "DISCOVERY_FAILED", "LLM_UNAVAILABLE", "LLM_INVALID_OUTPUT",
    "INTENT_UNKNOWN", "SEM_UNKNOWN_KEY", "SEM_TYPE", "SEM_UNSUPPORTED_OP", "NOTION_4XX",
    "NOTION_5XX", "UNDO_EXPIRED", "UNDO_FAILED", "SESSION_EXPIRED", "nothing_to_write",
    "item_not_found",
]

# Superset of every placeholder name used across ERRORS/QUESTION/DONE_* templates. str.format
# silently ignores kwargs a template doesn't reference, so one shared dict can probe them all.
ALL_KWARGS = dict(
    target_name="Покупки", item_title="Молоко", url="https://notion.so/x",
    field_name="Приоритет", value="A", intent="create", item_text="кефир",
    message="value_invalid", minutes=5,
)


def test_every_qtype_has_a_question_template():
    for qtype in get_args(QType):
        assert qtype in texts.QUESTION
        assert texts.QUESTION[qtype].strip()


def test_every_documented_error_code_has_a_message():
    for code in ERROR_CODES:
        assert code in texts.ERRORS
        assert texts.ERRORS[code].strip()


def test_no_unsubstituted_placeholders_after_formatting():
    templates = (
        list(texts.QUESTION.items()) + list(texts.QUESTION_WITH_TARGET.items())
        + list(texts.ERRORS.items())
    )
    for name, value in templates:
        formatted = value.format(**ALL_KWARGS)
        assert "{" not in formatted, f"{name!r} left an unfilled placeholder: {formatted!r}"


def test_question_with_target_covers_exactly_the_documented_types():
    # The FLOWS.md wording that names the target ("...в «Покупки»"); the other question types
    # either don't mention the target (target, item_not_found, date, field_confirm,
    # intent_confirm) or resolve it entirely through their option buttons (field_ambiguous).
    assert set(texts.QUESTION_WITH_TARGET) == {
        "item", "field_required", "content_required", "nothing_to_write",
    }


def test_done_templates_format_cleanly():
    for tmpl in (texts.DONE_CREATE_ITEM, texts.DONE_UPDATE, texts.DONE_CREATE_PAGE,
                 texts.DONE_APPEND, texts.DONE_LINK):
        assert "{" not in tmpl.format(**ALL_KWARGS)


def test_button_labels_are_exact():
    assert texts.BTN_CANCEL == "Отмена"
    assert texts.BTN_INBOX == "В разное"
    assert texts.BTN_UNDO == "Отменить"
    assert texts.BTN_CONFIRM == "Да"
    assert texts.BTN_OTHER == "Другое"
    assert texts.BTN_ADD_NEW == "Добавить как новое"


def test_standalone_strings_are_nonempty():
    for s in (texts.INBOX_SAVED, texts.INBOX_SAVED_EXPIRED, texts.INBOX_FAILED, texts.CANCELLED,
              texts.UNDONE, texts.SEARCH_EMPTY, texts.SEARCH_HEADER, texts.UNTITLED):
        assert isinstance(s, str) and s.strip()
