"""app.telegram.keyboards.to_markup: renders a transport-neutral Reply's button rows into a
telegram.InlineKeyboardMarkup, passing callback data through byte-for-byte (the orchestrator
parses these ids; a mismatch would misroute a button)."""

from __future__ import annotations

import pytest
from telegram import InlineKeyboardMarkup

from app.conversation.reply import Button, Reply
from app.telegram.keyboards import to_markup


def test_two_option_rows_plus_trailing_row_round_trip_same_shape():
    reply = Reply(
        text="q",
        buttons=[
            [Button("a:tok:opt1", "Опция 1")],
            [Button("a:tok:opt2", "Опция 2")],
            [Button("a:tok:cancel", "Отмена"), Button("a:tok:inbox", "В разное")],
        ],
    )
    markup = to_markup(reply)
    assert isinstance(markup, InlineKeyboardMarkup)
    shape = [[(b.text, b.callback_data) for b in row] for row in markup.inline_keyboard]
    assert shape == [
        [("Опция 1", "a:tok:opt1")],
        [("Опция 2", "a:tok:opt2")],
        [("Отмена", "a:tok:cancel"), ("В разное", "a:tok:inbox")],
    ]


def test_callback_data_is_byte_identical_to_button_id():
    weird_id = "a:tok:opt-with-dashes_and.dots"
    reply = Reply(text="q", buttons=[[Button(weird_id, "label")]])
    markup = to_markup(reply)
    assert markup.inline_keyboard[0][0].callback_data == weird_id


def test_no_buttons_returns_none():
    reply = Reply(text="just text")
    assert to_markup(reply) is None


def test_65_byte_id_raises():
    too_long = "a:" + ("x" * 63)  # 65 bytes total
    assert len(too_long.encode()) == 65
    reply = Reply(text="q", buttons=[[Button(too_long, "label")]])
    with pytest.raises(AssertionError):
        to_markup(reply)


def test_64_byte_id_is_fine():
    exactly_64 = "a:" + ("x" * 62)
    assert len(exactly_64.encode()) == 64
    reply = Reply(text="q", buttons=[[Button(exactly_64, "label")]])
    markup = to_markup(reply)
    assert markup.inline_keyboard[0][0].callback_data == exactly_64
