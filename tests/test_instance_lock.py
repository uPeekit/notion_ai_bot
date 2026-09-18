import os
import subprocess
import sys
import textwrap

import pytest

from app.instance_lock import AlreadyRunning, InstanceLock, is_locked


def test_acquire_writes_pid_and_second_acquire_is_refused(tmp_path):
    path = tmp_path / "data" / "bot.pid"
    first = InstanceLock(path)
    first.acquire()
    try:
        assert path.read_text(encoding="utf-8") == str(os.getpid())
        with pytest.raises(AlreadyRunning):
            InstanceLock(path).acquire()
        assert path.read_text(encoding="utf-8") == str(os.getpid())  # loser did not truncate
    finally:
        first.release()


def test_release_lets_the_next_instance_start(tmp_path):
    path = tmp_path / "bot.pid"
    first = InstanceLock(path)
    first.acquire()
    first.release()
    second = InstanceLock(path)
    second.acquire()  # does not raise
    second.release()


def test_leftover_file_from_a_dead_process_does_not_block(tmp_path):
    path = tmp_path / "bot.pid"
    path.write_text("99999", encoding="utf-8")  # what a crash leaves behind
    assert not is_locked(path)
    lock = InstanceLock(path)
    lock.acquire()
    lock.release()


def test_is_locked_tracks_the_holder(tmp_path):
    path = tmp_path / "bot.pid"
    assert not is_locked(path)  # no file at all
    lock = InstanceLock(path)
    lock.acquire()
    try:
        assert is_locked(path)
    finally:
        lock.release()
    assert not is_locked(path)


def test_release_twice_is_harmless(tmp_path):
    lock = InstanceLock(tmp_path / "bot.pid")
    lock.acquire()
    lock.release()
    lock.release()


def test_lock_held_by_another_process_is_seen_and_dropped_when_it_dies(tmp_path):
    """The case that matters in production: a different process, not a second handle."""
    path = tmp_path / "bot.pid"
    holder = textwrap.dedent(f"""
        import sys, time
        from pathlib import Path
        from app.instance_lock import InstanceLock
        lock = InstanceLock(Path({str(path)!r}))  # kept referenced: GC would close the handle
        lock.acquire()
        print("locked", flush=True)
        sys.stdin.readline()
    """)
    proc = subprocess.Popen([sys.executable, "-c", holder], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        assert is_locked(path)
        with pytest.raises(AlreadyRunning):
            InstanceLock(path).acquire()
    finally:
        proc.stdin.close()  # the holder exits without releasing, like a crash
        proc.wait(timeout=30)
    assert not is_locked(path)
