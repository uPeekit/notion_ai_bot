"""Structured logging for the whole process.

Every record gets the audit `event_id` of the turn that produced it, via a contextvars-backed
filter, so a log line can always be traced back to a row in `events` — `-` when no turn is in
flight (startup, the admin server thread, a message that never reached `Orchestrator._turn`).

`configure()` takes only a level string, on purpose: it must never see a `Settings` object, so it
cannot log a token by reading one out of settings itself. That is not the same as saying no token
can ever reach a record through this module — it also has to actively defend against one
specific, real leak: `httpx` (python-telegram-bot's own HTTP client) logs each request's full URL
at INFO by default, and PTB's own requests embed the bot token directly in that URL
(`.../bot<token>/getUpdates`), so at the app's normal `INFO` level every poll would otherwise
print the token to stderr on its own, with no app code ever calling `log.info` on it. `configure`
forces the `httpx`/`httpcore` loggers to `WARNING` unconditionally, regardless of the level it is
given, so raising the app's own log level can never accidentally turn this back on.

`bind_event` is a context manager, not a bare setter, because the id must always come back off
again once the turn that owns it ends — otherwise a later, unrelated log record (on the same
thread, or the same asyncio Task if the caller reuses one) would carry a stale id. It is set from
exactly one place: `Orchestrator._turn`, wrapped around the call to `work(turn)`, because the
`events` row — and so the id itself — only exists once `_open` has returned; nothing upstream of
that point (a Telegram handler receiving an update) can know it in advance.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

FORMAT = "%(asctime)s %(levelname)s %(name)s [event=%(event_id)s] %(message)s"
NO_EVENT = "-"

event_id_var: ContextVar[str] = ContextVar("event_id", default=NO_EVENT)

# Third-party loggers whose own default INFO output is unsafe here: httpx logs "<method> <url>"
# for every request, and python-telegram-bot's requests carry the bot token in that URL's path.
# Held at WARNING no matter what level configure() itself is given.
_THIRD_PARTY_WARNING_ONLY = ("httpx", "httpcore")


class _EventIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.event_id = event_id_var.get()
        return True


def configure(level: str = "INFO") -> None:
    """(Re)configures the root logger: one stderr handler carrying the event_id filter and the
    format above, plus the httpx/httpcore silencing described in the module docstring. Idempotent
    — safe to call more than once (tests do), since it replaces rather than accumulates
    handlers."""
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.addFilter(_EventIdFilter())
    handler.setFormatter(logging.Formatter(FORMAT))
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
