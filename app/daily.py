"""The message the bot sends on its own: the morning agenda.

One asyncio task that sleeps until the next occurrence of a wall-clock time in the user's own
time zone, sends, and sleeps again. Time zones and daylight saving are handled by computing the
next run from the current local time every round rather than by adding 24 hours to the last
one."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

# How far past the scheduled minute a run may still fire (a laptop waking from sleep, a bot
# restarted at 09:04). Later than this, the digest waits for tomorrow — nobody wants yesterday's
# agenda at six in the evening.
GRACE = timedelta(hours=2)


def parse_at(value: str) -> time | None:
    """"09:00" -> time(9, 0). Empty or malformed: no schedule at all, and a warning."""
    text = value.strip()
    if not text:
        return None
    try:
        hour, minute = (int(part) for part in text.split(":", 1))
        return time(hour=hour, minute=minute)
    except (ValueError, TypeError):
        log.warning("bad daily time %r: expected HH:MM", value)
        return None


def parse_times(value: str) -> tuple[time, ...]:
    """"12:00,19:00" -> two times a day, in order. Bad entries are dropped with a warning."""
    found = [parse_at(part) for part in value.split(",")]
    return tuple(sorted({t for t in found if t is not None}))


class DailyMessage:
    """Calls `send()` once a day at `at`, in `tz`. `send` decides whether there is anything
    worth sending; this class only decides when."""

    def __init__(self, send: Callable[[], Awaitable[object]], at: time | tuple[time, ...],
                 tz: str, *, now: Callable[[], datetime] | None = None) -> None:
        self._send = send
        self._times = (at,) if isinstance(at, time) else tuple(sorted(at))
        self._zone = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._zone))
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def next_run(self, after: datetime) -> datetime:
        """The next moment the message is due, strictly after `after` — the earliest of the
        configured times today, or the first one tomorrow."""
        local = after.astimezone(self._zone)
        candidates = [local.replace(hour=t.hour, minute=t.minute, second=0, microsecond=0)
                      for t in self._times]
        later = [c for c in candidates if c > after]
        return min(later) if later else min(candidates) + timedelta(days=1)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        # A bot started shortly after the time still sends today's (GRACE), so a restart at
        # 09:05 does not silently skip the day.
        now = self._now()
        # The most recent time that has already passed today (or yesterday's last one).
        passed = [now.astimezone(self._zone).replace(hour=t.hour, minute=t.minute, second=0,
                                                      microsecond=0) for t in self._times]
        previous = max([p for p in passed if p <= now], default=min(passed) - timedelta(days=1))
        if now - previous < GRACE:
            await self._fire()
        while True:
            now = self._now()
            wait = (self.next_run(now) - now).total_seconds()
            await asyncio.sleep(max(wait, 1.0))
            await self._fire()

    async def _fire(self) -> None:
        try:
            await self._send()
        except Exception:  # a failed digest must never stop the schedule
            log.exception("daily message failed")
