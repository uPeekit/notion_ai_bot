"""app.logging_setup: the event_id filter/contextvar and configure()'s isolation from Settings."""

from __future__ import annotations

import inspect
import logging

from app import logging_setup
from app.logging_setup import bind_event, configure, event_id_var


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


def test_configure_has_no_access_to_settings_or_tokens():
    """configure() must be unable to log a token even by accident: it never receives a Settings
    object or any secret-shaped argument, only a level string. A formatter/filter that never sees
    settings cannot leak one of its values into a record."""
    params = inspect.signature(configure).parameters
    assert list(params) == ["level"]
    assert params["level"].annotation in (str, "str")


def _reset_root_and_third_party() -> None:
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)
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
