"""app.telegram.auth: the allowlist gate. Checked before anything else touches an update — before
the text is read, before transcription, before any audit row exists. An unauthorised user gets no
reply at all, only a WARNING log with ids and nothing else (documentation/ERRORS.md AUTH_DENIED,
FLOWS.md F16)."""

from __future__ import annotations

import logging

from app.config import Settings
from app.telegram.auth import allowed_filter, deny, is_allowed


def test_allowed_id_passes():
    assert is_allowed(1, frozenset({1, 2})) is True


def test_unknown_id_denied():
    assert is_allowed(99, frozenset({1, 2})) is False


def test_none_denied():
    assert is_allowed(None, frozenset({1, 2})) is False


def test_empty_allowlist_denies_everyone():
    assert is_allowed(1, frozenset()) is False


def test_deny_logs_warning_with_ids_only(caplog):
    with caplog.at_level(logging.WARNING):
        deny(99, 12345)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.levelno == logging.WARNING
    assert "99" in record.getMessage()
    assert "12345" in record.getMessage()


def test_deny_record_contains_no_text_or_token(caplog):
    """The log record must never carry message content or a token — only the two ids."""
    with caplog.at_level(logging.WARNING):
        deny(99, 12345)
    message = caplog.records[0].getMessage()
    assert "text" not in message.lower()
    assert "token" not in message.lower()


def test_deny_handles_missing_chat(caplog):
    """A channel post or similarly chat-less update: deny must not crash on a None chat id."""
    with caplog.at_level(logging.WARNING):
        deny(None, None)
    assert len(caplog.records) == 1


def test_allowed_filter_accepts_allowed_user(env):
    settings = Settings(_env_file=None)
    assert settings.allowed_user_ids == frozenset({1, 2})
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=1)
    assert f.filter(message) is True


def test_allowed_filter_rejects_unknown_user(env):
    settings = Settings(_env_file=None)
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=99)
    assert f.filter(message) is False


def test_allowed_filter_rejects_no_user(env):
    settings = Settings(_env_file=None)
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=None)
    assert f.filter(message) is False


def test_allowed_filter_empty_allowlist_denies_everyone(env, monkeypatch):
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    settings = Settings(_env_file=None)
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=1)
    assert f.filter(message) is False


def test_allowed_filter_denied_user_logs_auth_denied(env, caplog):
    """The declarative gate must log, exactly like _guard_callback does for the one handler it
    cannot cover: a denied text message, voice note or command would otherwise leave no trace at
    all (filters.User's own membership test just returns False, silently)."""
    settings = Settings(_env_file=None)
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=99, chat_id=555)
    with caplog.at_level(logging.WARNING):
        result = f.filter(message)
    assert result is False
    denied = [r for r in caplog.records if "AUTH_DENIED" in r.getMessage()]
    assert len(denied) == 1
    assert "99" in denied[0].getMessage()
    assert "555" in denied[0].getMessage()


def test_allowed_filter_allowed_user_logs_nothing(env, caplog):
    settings = Settings(_env_file=None)
    f = allowed_filter(settings)
    message = _FakeMessage(user_id=1)
    with caplog.at_level(logging.WARNING):
        result = f.filter(message)
    assert result is True
    assert not any("AUTH_DENIED" in r.getMessage() for r in caplog.records)


class _FakeUser:
    def __init__(self, user_id: int):
        self.id = user_id


class _FakeMessage:
    """Just enough of telegram.Message for filters.User.filter and _AllowedUserFilter.filter: a
    .from_user with an .id, plus an optional .chat_id."""

    def __init__(self, user_id: int | None, chat_id: int | None = None):
        self.from_user = _FakeUser(user_id) if user_id is not None else None
        self.chat_id = chat_id
