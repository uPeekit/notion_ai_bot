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


def allowed_filter(settings: Settings) -> filters.BaseFilter:
    """A python-telegram-bot filter built from Settings.allowed_user_ids, so an unauthorised
    update never reaches a handler callback. An empty allowlist denies everyone (filters.User's
    allow_empty defaults to False)."""
    return filters.User(user_id=settings.allowed_user_ids)
