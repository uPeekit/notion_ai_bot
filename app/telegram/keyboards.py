"""Renders a transport-neutral Reply's button rows into a telegram.InlineKeyboardMarkup.
Callback data is passed through byte-for-byte from Button.id: the orchestrator parses these ids
(a:<token>:<option_id>, u:<execution_id>, i:<event_id>), so this must never re-encode, truncate
or prefix them."""

from __future__ import annotations

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from app.conversation.reply import Reply

_TELEGRAM_CALLBACK_DATA_LIMIT = 64


def to_markup(reply: Reply) -> InlineKeyboardMarkup | None:
    """None when the reply has no buttons. Enforces the invariant the orchestrator already
    guarantees (an id fits Telegram's 64-byte callback_data limit); a longer id is an upstream
    programming error and must raise loudly rather than silently truncate. A plain `assert`
    would be stripped under `python -O`, so this raises explicitly instead."""
    if not reply.buttons:
        return None
    rows = []
    for row in reply.buttons:
        rendered = []
        for button in row:
            size = len(button.id.encode())
            if size > _TELEGRAM_CALLBACK_DATA_LIMIT:
                raise ValueError(
                    f"callback_data {button.id!r} is {size} bytes, over Telegram's "
                    f"{_TELEGRAM_CALLBACK_DATA_LIMIT}-byte limit"
                )
            rendered.append(InlineKeyboardButton(button.label, callback_data=button.id))
        rows.append(rendered)
    return InlineKeyboardMarkup(rows)
