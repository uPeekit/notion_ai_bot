"""How much Notion one turn cost, for the log line at the end of it.

A single message can quietly become a dozen HTTP calls — discovery, the page being written,
the blocks read to find a list — and the terminal used to show none of it. The tally lives in
a context variable holding one mutable dict per turn, so calls made inside `asyncio.gather`
(which copies the context) still count against the turn that started them.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

Tally = dict[str, int]

_calls: ContextVar[Tally | None] = ContextVar("notion_calls", default=None)


@contextmanager
def collect() -> Iterator[Tally]:
    """Counts Notion calls made in this block. The dict fills as they happen."""
    tally: Tally = {}
    token = _calls.set(tally)
    try:
        yield tally
    finally:
        _calls.reset(token)


def record(method: str, path: str) -> None:
    """One call, named the way the log should show it: the API collection it went to
    ("pages", "blocks", "data_sources"), which is all the summary needs."""
    tally = _calls.get()
    if tally is None:
        return  # outside a turn (startup checks, the sweeper): nothing is being summarised
    part = path.lstrip("/").split("/", 1)[0].split("?", 1)[0] or "root"
    tally[f"{method.lower()} {part}"] = tally.get(f"{method.lower()} {part}", 0) + 1


def summary(tally: Tally) -> str:
    """"7 calls: get pages 3, post search 2, patch blocks 2" — empty when nothing was called."""
    if not tally:
        return ""
    total = sum(tally.values())
    parts = ", ".join(f"{name} {n}" for name, n in
                      sorted(tally.items(), key=lambda kv: (-kv[1], kv[0])))
    return f"{total} calls: {parts}"
