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


class DailyMessage:
    """Calls `send()` once a day at `at`, in `tz`. `send` decides whether there is anything
    worth sending; this class only decides when."""

    def __init__(self, send: Callable[[], Awaitable[object]], at: time, tz: str,
                 *, now: Callable[[], datetime] | None = None) -> None:
        self._send = send
        self._at = at
        self._zone = ZoneInfo(tz)
        self._now = now or (lambda: datetime.now(self._zone))
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def next_run(self, after: datetime) -> datetime:
        """The next moment the message is due, strictly after `after`."""
        today = after.astimezone(self._zone).replace(
            hour=self._at.hour, minute=self._at.minute, second=0, microsecond=0)
        return today if today > after else today + timedelta(days=1)

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
        due_today = self.next_run(now) - timedelta(days=1)
        if now - due_today < GRACE:
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
