"""The allowlist gate: checked before anything else touches an update — before the text is read,
before transcription, before any audit row exists (documentation/ERRORS.md AUTH_DENIED,
FLOWS.md F16). An unauthorised user gets no reply at all, only a WARNING log carrying ids and
nothing else — never message text, never a token."""

from __future__ import annotations

import logging

from telegram.ext import filters

from app.config import Settings

log = logging.getLogger(__name__)


def is_allowed(user_id: int | None, allowed: frozenset[int]) -> bool:
    """None (channel posts, edited service messages with no sender) is never allowed."""
    return user_id is not None and user_id in allowed


def deny(user_id: int | None, chat_id: int | None) -> None:
    """Logs the denial at WARNING with the ids only — no message text, no token."""
    log.warning("AUTH_DENIED user_id=%s chat_id=%s", user_id, chat_id)


class _AllowedUserFilter(filters.MessageFilter):
    """The declarative gate: a plain filters.User would reject a denied sender silently (its
    membership test just returns False), leaving text messages, voice notes and commands denied
    with no trace in the log at all — only app.telegram.handlers._guard_callback (the one
    handler PTB cannot gate this way) would ever call deny(). This wraps the same membership
    test and calls deny() on the way to returning False, so every declaratively-gated handler
    logs exactly like the callback path does, without moving the check into a handler body."""

    def __init__(self, allowed: frozenset[int]) -> None:
        super().__init__()
        self._allowed = allowed

    def filter(self, message) -> bool:
        user = message.from_user
        user_id = user.id if user else None
        if is_allowed(user_id, self._allowed):
            return True
        deny(user_id, getattr(message, "chat_id", None))
        return False


def allowed_filter(settings: Settings) -> filters.BaseFilter:
    """A python-telegram-bot filter built from Settings.allowed_user_ids, so an unauthorised
    update never reaches a handler callback. An empty allowlist denies everyone (is_allowed(_,
    frozenset()) is always False)."""
    return _AllowedUserFilter(settings.allowed_user_ids)
