"""One bot per install.

Two copies polling the same Telegram token make Telegram answer both with `409 Conflict`, and two
copies sharing one SQLite file race each other's sessions — and double-clicking `start.cmd` twice
is the easiest mistake to make. So the process holds an OS-level lock on `data/bot.pid` for its
whole lifetime.

The lock is a byte-range lock (`msvcrt.locking` on Windows, `flock` elsewhere), not the file's
existence: the OS drops it the moment the process dies, crash included, so a file left behind by a
crash never blocks the next start. `apply_update.py` probes the same byte with the same primitive
to refuse updating files under a running bot; it carries its own copy of `is_locked` because it
must not import `app` (it runs while `app` is being replaced).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class AlreadyRunning(Exception):
    """Another process holds the lock on this install's pid file."""


# Windows byte-range locks are mandatory: a locked byte cannot even be *read* through another
# handle. Locking one byte far past the pid keeps the pid itself readable (Windows allows locking
# beyond end-of-file). `flock` is advisory and whole-file, so the offset is irrelevant there.
LOCK_OFFSET = 1 << 30


def _lock(f: IO[str]) -> None:
    if os.name == "nt":
        f.seek(LOCK_OFFSET)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f: IO[str]) -> None:
    if os.name == "nt":
        f.seek(LOCK_OFFSET)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def is_locked(path: Path) -> bool:
    """True while some process holds the lock on `path`. A missing file, or one left behind by a
    process that has since exited, is not locked."""
    if not path.exists():
        return False
    try:
        f = open(path, "a+", encoding="utf-8")
    except OSError:
        return True
    try:
        try:
            _lock(f)
        except OSError:
            return True
        _unlock(f)
        return False
    finally:
        f.close()


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._f: IO[str] | None = None

    def acquire(self) -> None:
        """Takes the lock and writes this process's pid into the file; raises `AlreadyRunning`
        when another process holds it. Opened in append mode so a losing attempt never
        truncates the winner's pid."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        f = open(self._path, "a+", encoding="utf-8")
        try:
            _lock(f)
        except OSError:
            f.close()
            raise AlreadyRunning(str(self._path)) from None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        self._f = f

    def release(self) -> None:
        """Unlocks and closes. The file is deliberately left in place: deleting it would race a
        new process that takes the lock between our close and the unlink (it would then hold a
        lock on a file a third process can no longer see), and a leftover file is harmless —
        only the lock means anything."""
        if self._f is None:
            return
        f, self._f = self._f, None
        try:
            _unlock(f)
        except OSError:
            pass  # the handle is closing anyway; the OS drops the lock with it
        f.close()
