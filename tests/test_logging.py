"""app.logging_setup: the event_id filter/contextvar and configure()'s isolation from Settings."""

from __future__ import annotations

import inspect
import logging
import logging.handlers
from collections.abc import Sequence
from pathlib import Path

import pytest

from app import logging_setup
from app.logging_setup import REDACTED, bind_event, configure, event_id_var


def _emit(logger: logging.Logger) -> logging.LogRecord:
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.addFilter(logging_setup._EventIdFilter())
    logger.addHandler(handler)
    try:
        logger.warning("probe")
    finally:
        logger.removeHandler(handler)
    return records[0]


def test_record_inside_bind_event_carries_the_id():
    log = logging.getLogger("test.logging.inside")
    log.setLevel(logging.DEBUG)
    with bind_event(7):
        record = _emit(log)
    assert record.event_id == "7"


def test_record_outside_bind_event_carries_a_dash():
    log = logging.getLogger("test.logging.outside")
    log.setLevel(logging.DEBUG)
    record = _emit(log)
    assert record.event_id == "-"


def test_bind_event_restores_the_previous_value_on_exit():
    log = logging.getLogger("test.logging.nested")
    log.setLevel(logging.DEBUG)
    with bind_event(1):
        with bind_event(2):
            inner = _emit(log)
        outer_again = _emit(log)
    assert inner.event_id == "2"
    assert outer_again.event_id == "1"
    assert event_id_var.get() == "-"


def test_bind_event_none_is_the_dash():
    log = logging.getLogger("test.logging.none")
    log.setLevel(logging.DEBUG)
    with bind_event(None):
        record = _emit(log)
    assert record.event_id == "-"


def test_configure_has_no_access_to_settings():
    """configure() must be unable to *discover* a secret: it takes a level string and an explicit
    sequence of secret values to redact, never a Settings object it could read a third token out
    of. `redact` is keyword-only so a level can never land there by position."""
    params = inspect.signature(configure).parameters
    assert list(params) == ["level", "redact", "log_file"]
    assert params["level"].annotation in (str, "str")
    assert params["redact"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["redact"].annotation in (Sequence[str], "Sequence[str]")
    # A path, not a Settings: the file handler gets a location, never a way to find a secret.
    assert params["log_file"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["log_file"].annotation in (str | Path | None, "str | Path | None")
    assert not any("Settings" in str(p.annotation) for p in params.values())


def _reset_root_and_third_party() -> None:
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
        h.close()  # a file handler left open keeps its tmp_path file locked on Windows
    for name in logging_setup._THIRD_PARTY_WARNING_ONLY:
        logging.getLogger(name).setLevel(logging.NOTSET)


def test_configure_installs_the_event_id_filter_on_the_root_logger(capsys):
    configure("DEBUG")
    try:
        with bind_event(42):
            logging.getLogger("test.logging.configured").warning("hello")
        captured = capsys.readouterr()
        assert "[event=42]" in captured.err
    finally:
        _reset_root_and_third_party()


def test_configure_silences_httpx_and_httpcore_info_logging(capsys):
    """The real leak this guards against: python-telegram-bot's own httpx client logs
    "GET .../bot<token>/getUpdates" at INFO on every poll. Asserting on actual emitted output
    (not just configure()'s signature) is what proves the silencing happens, not just that it is
    plausible."""
    configure("DEBUG")  # even at DEBUG, httpx/httpcore must not get through
    try:
        logging.getLogger("httpx").info("GET http://example/bot123:SECRET-TOKEN/getUpdates")
        logging.getLogger("httpcore").info("connect_tcp.started host='example'")
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "SECRET-TOKEN" not in captured.err
    finally:
        _reset_root_and_third_party()


def test_configure_silences_extbot_debug_logging_even_at_debug(capsys):
    """A second, independent leak from a second, independent logger: python-telegram-bot's own
    `telegram.ext.ExtBot` (not its httpx layer — this fires from `ExtBot.__init__` itself) logs
    "Set Bot API URL: https://api.telegram.org/bot<token>" at DEBUG, every time
    `Application.builder().token(...).build()` constructs one — which `app.main.build()` always
    does. The httpx/httpcore floor above does nothing for this logger; it needs its own entry in
    `_THIRD_PARTY_WARNING_ONLY`. Regression test for a real leak this suite's own full-stack
    security test (`tests/test_security.py`) found: with `telegram.ext.ExtBot` missing from that
    tuple, this assertion fails and shows the token in plain text."""
    configure("DEBUG")
    try:
        logging.getLogger("telegram.ext.ExtBot").debug(
            "Set Bot API URL: https://api.telegram.org/bot123:SECRET-TOKEN"
        )
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "SECRET-TOKEN" not in captured.err
    finally:
        _reset_root_and_third_party()


# ---- redaction: the token must not reach stderr even from a logger nobody floored -------------

TOKEN = "123456:AA-really-secret-bot-token"


def test_configure_redacts_a_secret_in_a_message(capsys):
    """Half one of the Critical leak: python-telegram-bot's polling retry loop logs on
    `telegram.ext` — a logger that is deliberately *not* in `_THIRD_PARTY_WARNING_ONLY` and whose
    record here is an ERROR anyway, so no level floor can stop it. Only the redacting formatter
    can, and it has to cover the record's `%`-args, not just its format string."""
    configure("INFO", redact=[TOKEN, ""])
    try:
        logging.getLogger("telegram.ext").error("token %s rejected", TOKEN)
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()
    assert TOKEN not in err
    assert REDACTED in err
    assert "rejected" in err  # the diagnosis itself survives; only the value is gone


def test_configure_redacts_a_secret_inside_a_traceback(capsys):
    """Half two: the token reaches stderr through `exc_info`, not through the message at all —
    `telegram/_bot.py` re-wraps a 401 as InvalidToken("The token `<token>` was rejected by the
    server.") and `networkloop.py` logs that with `_LOGGER.exception`. Redacting `record.msg`
    alone would leave the traceback untouched, so this must be asserted separately."""
    configure("INFO", redact=[TOKEN])
    try:
        try:
            raise RuntimeError(f"The token `{TOKEN}` was rejected by the server.")
        except RuntimeError:
            logging.getLogger("telegram.ext").exception("Invalid token. Aborting retry loop.")
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()
    assert "Traceback" in err  # the traceback really was emitted, so the assertion below bites
    assert TOKEN not in err
    assert REDACTED in err


def test_configure_without_redact_still_emits_normally(capsys):
    configure("INFO")
    try:
        logging.getLogger("test.logging.noredact").warning("plain message")
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()
    assert "plain message" in err
    assert REDACTED not in err


def test_redaction_does_not_mutate_the_record():
    """Other handlers (caplog here, a file handler later) must still see what was logged: the
    formatter rewrites its own output string, never the shared LogRecord."""
    record = logging.LogRecord(
        "x", logging.WARNING, __file__, 1, "token %s", (TOKEN,), None
    )
    record.event_id = "-"
    formatted = logging_setup._RedactingFormatter(logging_setup.FORMAT, [TOKEN]).format(record)
    assert TOKEN not in formatted
    assert record.args == (TOKEN,)
    assert record.msg == "token %s"


# ---- level normalisation ----------------------------------------------------------------------


def test_configure_accepts_a_lowercase_level(capsys):
    """LOG_LEVEL is the one key the README invites the user to edit, and `LOG_LEVEL=debug` is the
    obvious way to mistype it. `Logger.setLevel("debug")` raises ValueError."""
    configure("debug")
    try:
        assert logging.getLogger().level == logging.DEBUG
    finally:
        _reset_root_and_third_party()


def test_configure_rejects_an_unknown_level_naming_the_valid_ones():
    with pytest.raises(ValueError) as exc:
        configure("verbose")
    message = str(exc.value)
    assert "verbose" in message
    for name in logging_setup.LEVELS:
        assert name in message


def test_log_file_receives_records_with_the_event_id(tmp_path):
    log_file = tmp_path / "logs" / "bot.log"  # parent does not exist yet
    configure("INFO", log_file=log_file)
    try:
        with bind_event(7):
            logging.getLogger("test.logging.file").info("written to disk")
    finally:
        _reset_root_and_third_party()
    text = log_file.read_text(encoding="utf-8")
    assert "written to disk" in text
    assert "[event=7]" in text


def test_log_file_is_redacted_like_stderr(tmp_path):
    """The file is where a leaked token would outlive the console window, so it must get the
    same redacting formatter — message, args and traceback alike."""
    log_file = tmp_path / "bot.log"
    secret = "7654321:AAH-FILE-SECRET"
    configure("INFO", redact=(secret,), log_file=log_file)
    try:
        log = logging.getLogger("test.logging.file.redact")
        log.error("token in message %s", secret)
        try:
            raise RuntimeError(f"The token `{secret}` was rejected by the server.")
        except RuntimeError:
            log.exception("token in traceback")
    finally:
        _reset_root_and_third_party()
    text = log_file.read_text(encoding="utf-8")
    assert "Traceback" in text  # the traceback really was written, so the check is not vacuous
    assert secret not in text
    assert REDACTED in text


def test_log_file_rotates_within_its_bounds(tmp_path):
    configure("INFO", log_file=tmp_path / "bot.log")
    try:
        handlers = [h for h in logging.getLogger().handlers
                    if isinstance(h, logging.handlers.RotatingFileHandler)]
    finally:
        _reset_root_and_third_party()
    assert len(handlers) == 1
    assert handlers[0].maxBytes == logging_setup.LOG_FILE_MAX_BYTES
    assert handlers[0].backupCount == logging_setup.LOG_FILE_BACKUPS


def test_reconfigure_closes_the_previous_log_file(tmp_path):
    first = tmp_path / "first.log"
    configure("INFO", log_file=first)
    handler = next(h for h in logging.getLogger().handlers
                   if isinstance(h, logging.FileHandler))
    configure("INFO", log_file=tmp_path / "second.log")
    try:
        assert handler.stream is None  # closed, not merely detached
        assert sum(isinstance(h, logging.FileHandler)
                   for h in logging.getLogger().handlers) == 1
    finally:
        _reset_root_and_third_party()


def test_no_log_file_means_stderr_only(tmp_path):
    configure("INFO")
    try:
        assert not any(isinstance(h, logging.FileHandler)
                       for h in logging.getLogger().handlers)
    finally:
        _reset_root_and_third_party()


def test_rejected_token_logs_one_line_not_a_traceback(capsys):
    """A mistyped bot token is the most likely first-run mistake. PTB logs it at ERROR with a
    ~40-line traceback; the user needs the one line, and app.main prints the remedy after it."""
    from telegram.error import InvalidToken

    configure("INFO", redact=[TOKEN])
    try:
        try:
            raise InvalidToken(f"The token `{TOKEN}` was rejected by the server.")
        except InvalidToken:
            logging.getLogger("telegram.ext").exception("Invalid token. Aborting retry loop.")
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()
    assert "Invalid token. Aborting retry loop." in err
    assert "Traceback" not in err
    assert TOKEN not in err
