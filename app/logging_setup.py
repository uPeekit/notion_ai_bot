"""Structured logging for the whole process.

Every record gets the audit `event_id` of the turn that produced it, via a contextvars-backed
filter, so a log line can always be traced back to a row in `events` — `-` when no turn is in
flight (startup, the admin server thread, a message that never reached `Orchestrator._turn`).

`configure()` takes a level string and, separately, the *values* of the secrets that must never
be printed — never a `Settings` object. The distinction is deliberate: a formatter handed two
opaque strings cannot go looking for a third secret it was not given, and this module still has
no way to read a token out of settings on its own. That is not the same as saying no token can
ever reach a record through this module — it also has to actively defend against three specific,
real leaks, from three independent third-party loggers, at three different levels:

* `httpx` (python-telegram-bot's own HTTP client) logs each request's full URL at INFO by
  default, and PTB's own requests embed the bot token directly in that URL
  (`.../bot<token>/getUpdates`), so at the app's normal `INFO` level every poll would otherwise
  print the token to stderr on its own, with no app code ever calling `log.info` on it.
* `telegram.ext.ExtBot` (PTB's own `Bot`/`ExtBot` class, not its HTTP layer) logs
  `"Set Bot API URL: <url>"` and `"...API File URL: <url>"` — both with the token baked into the
  URL the same way — once, at DEBUG, from its own constructor (`ExtBot.__init__`, always run by
  `Application.builder().token(...).build()`). This one is not an httpx request at all, so the
  httpx/httpcore floor below does nothing for it, and it fires long before polling ever starts —
  the very first thing `app.main.build()` does with a real token.

* `telegram.ext`'s polling retry loop (`telegram/ext/_utils/networkloop.py`) logs
  `_LOGGER.exception("... Invalid token. Aborting retry loop.")` — ERROR, *with* the traceback —
  when Telegram rejects the token, and the `InvalidToken` in that traceback carries the token
  itself: `telegram/_bot.py` re-wraps a 401 as ``InvalidToken(f"The token `{self._token}` was
  rejected by the server.")``. Unlike the two leaks above this one cannot be silenced by a level
  floor (it is an ERROR, and `telegram.ext` is a logger the app genuinely wants to hear from), so
  it is the reason the redacting formatter below exists at all.

`configure` forces `httpx`/`httpcore`/`telegram.ext.ExtBot` (and, in case a caller ever builds a
plain `telegram.Bot` instead of going through `Application.builder()`, `telegram.Bot` too) to
`WARNING` unconditionally, regardless of the level it is given, so raising the app's own log
level — including to `DEBUG` — can never accidentally turn either of these back on. On top of
that floor, `_RedactingFormatter` rewrites every occurrence of a known secret in the *formatted*
record — the message, its interpolated args and any traceback alike — to `***`, which is what
catches a leak arriving from a logger nobody thought to floor.

`bind_event` is a context manager, not a bare setter, because the id must always come back off
again once the turn that owns it ends — otherwise a later, unrelated log record (on the same
thread, or the same asyncio Task if the caller reuses one) would carry a stale id. It is set from
exactly one place: `Orchestrator._turn`, wrapped around the call to `work(turn)`, because the
`events` row — and so the id itself — only exists once `_open` has returned; nothing upstream of
that point (a Telegram handler receiving an update) can know it in advance.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

FORMAT = "%(asctime)s %(levelname)s %(name)s [event=%(event_id)s] %(message)s"
NO_EVENT = "-"
REDACTED = "***"

# The level names configure() accepts, in the order the error message lists them. NOTSET is
# deliberately absent: "no level" is not a log level a user can mean.
LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG")
# Five files of 5 MB: weeks of normal use at INFO, and a hard ceiling on disk use when
# something starts logging in a loop.
LOG_FILE_MAX_BYTES = 5 * 1024 * 1024
LOG_FILE_BACKUPS = 5

event_id_var: ContextVar[str] = ContextVar("event_id", default=NO_EVENT)

# Third-party loggers that would otherwise print the bot token to stderr on their own: httpx logs
# "<method> <url>" for every request (INFO), and telegram.ext.ExtBot logs the same token-bearing
# URL once at construction time (DEBUG, via its "Set Bot API URL" lines) — see the module
# docstring. Held at WARNING no matter what level configure() itself is given.
_THIRD_PARTY_WARNING_ONLY = ("httpx", "httpcore", "telegram.ext.ExtBot", "telegram.Bot")


class _EventIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.event_id = event_id_var.get()
        return True


class _RedactingFormatter(logging.Formatter):
    """The last line of defence: whatever a record says, no secret *value* leaves this handler.

    Redaction happens on the fully formatted string rather than on `record.msg`, because that is
    the only place all three carriers of a leak meet: the format string itself, the `%`-args
    interpolated into it, and the exception traceback `logging.Formatter.format` appends after
    the message. The record object is never mutated — other handlers (a test's `caplog`, a future
    file handler with its own policy) see it exactly as it was logged.

    `secrets` holds values, never names: an empty string would match everywhere and is dropped.
    """

    def __init__(self, fmt: str, secrets: Sequence[str] = ()) -> None:
        super().__init__(fmt)
        self._secrets = tuple(dict.fromkeys(s for s in secrets if s))

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        return text

    def formatException(self, ei) -> str:  # noqa: N802 (stdlib name)
        """A rejected bot token is a config mistake, not a crash: PTB logs it with a ~40-line
        traceback before `app.main` prints the one line that says what to fix. The message line
        ("Invalid token. Aborting retry loop.") is kept; only the traceback goes. Matched by name
        so this module never imports telegram. An empty result is falsy, so `Formatter.format`
        neither appends nor caches it, and other handlers still see the full exception."""
        exc = ei[1]
        if (exc is not None and type(exc).__name__ == "InvalidToken"
                and type(exc).__module__ == "telegram.error"):
            return ""
        return super().formatException(ei)


def configure(
    level: str = "INFO", *, redact: Sequence[str] = (), log_file: str | Path | None = None,
) -> None:
    """(Re)configures the root logger: one stderr handler carrying the event_id filter, the
    format above and a `_RedactingFormatter` over `redact`, plus the httpx/httpcore silencing
    described in the module docstring. Idempotent — safe to call more than once (tests do), since
    it replaces rather than accumulates handlers.

    `level` is case-insensitive (`LOG_LEVEL=info` in a hand-edited `.env` is the obvious mistake,
    and `Logger.setLevel` would raise a bare `ValueError` on it); an unknown name raises a
    `ValueError` naming every valid one, which `app.main` turns into a config exit.

    `redact` is a sequence of secret *values* — `app.main` passes the Telegram and Notion token
    values, nothing else. It is keyword-only and deliberately not a `Settings`: this module must
    stay unable to discover a secret it was not explicitly handed.

    `log_file`, when given, adds a rotating file handler carrying the *same* filter and the same
    redacting formatter — a file is where a leaked token would outlive the console window, so it
    must never get a cheaper formatter than stderr does.
    """
    name = (level or "").strip().upper()
    if name not in LEVELS:
        raise ValueError(f"unknown LOG_LEVEL {level!r}; expected one of {', '.join(LEVELS)}")
    root = logging.getLogger()
    root.setLevel(name)
    for h in list(root.handlers):
        root.removeHandler(h)
        if isinstance(h, logging.FileHandler):
            h.close()  # reconfiguring must not leak an open file (or keep it locked on Windows)
    handlers: list[logging.Handler] = [logging.StreamHandler(stream=sys.stderr)]
    if log_file:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.handlers.RotatingFileHandler(
            path, maxBytes=LOG_FILE_MAX_BYTES, backupCount=LOG_FILE_BACKUPS, encoding="utf-8",
        ))
    for handler in handlers:
        handler.addFilter(_EventIdFilter())
        handler.setFormatter(_RedactingFormatter(FORMAT, redact))
        root.addHandler(handler)
    for name in _THIRD_PARTY_WARNING_ONLY:
        logging.getLogger(name).setLevel(logging.WARNING)


@contextmanager
def bind_event(event_id: int | None) -> Iterator[None]:
    """Binds `event_id_var` for the duration of the block, resetting it to whatever it was
    before on exit (never simply back to NO_EVENT, so nested turns — there are none today, but a
    future sub-call that opens its own row would nest correctly)."""
    token = event_id_var.set(NO_EVENT if event_id is None else str(event_id))
    try:
        yield
    finally:
        event_id_var.reset(token)
