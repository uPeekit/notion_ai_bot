import sqlite3
import threading
from datetime import UTC, datetime, timedelta

import pytest

from app.audit.store import AuditStore


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path / "t.sqlite")
    s.migrate()
    yield s
    s.close()


def test_event_insert_update_read(store):
    eid = store.new_event(telegram_user_id=1, chat_id=1, kind="text", raw_input="купи хлеб")
    store.update_event(eid, decision="EXECUTE", executed=1, notion_page_id="p1", duration_ms=42)
    row = store.get_event(eid)
    assert row["raw_input"] == "купи хлеб"
    assert row["decision"] == "EXECUTE"
    assert row["executed"] == 1
    assert row["ts"].endswith("+00:00")


def test_unknown_column_rejected(store):
    with pytest.raises(ValueError):
        store.new_event(telegram_user_id=1, chat_id=1, kind="text", notion_token="x")


def test_session_roundtrip_and_expiry(store):
    now = datetime.now(UTC)
    store.save_session(7, '{"a":1}', now + timedelta(minutes=5))
    assert store.get_session(7, now) == '{"a":1}'
    assert store.get_session(7, now + timedelta(minutes=6)) is None
    assert store.get_session(7, now) is None  # expired row deleted


def test_session_replace_and_delete(store):
    now = datetime.now(UTC)
    store.save_session(7, "a", now + timedelta(minutes=5))
    store.save_session(7, "b", now + timedelta(minutes=5))
    assert store.get_session(7, now) == "b"
    store.delete_session(7)
    assert store.get_session(7, now) is None


def test_expired_sessions_returns_only_rows_at_or_past_expiry(store):
    now = datetime.now(UTC)
    store.save_session(1, "expired", now - timedelta(seconds=1))
    store.save_session(2, "exactly-now", now)
    store.save_session(3, "future", now + timedelta(minutes=5))
    rows = store.expired_sessions(now)
    assert sorted(rows) == [(1, "expired"), (2, "exactly-now")]


def test_expired_sessions_does_not_delete(store):
    now = datetime.now(UTC)
    store.save_session(1, "expired", now - timedelta(seconds=1))
    store.expired_sessions(now)
    assert store.get_session(1, now - timedelta(minutes=5)) == "expired"


def test_pop_session_returns_payload_and_removes_row(store):
    now = datetime.now(UTC)
    store.save_session(5, '{"a":1}', now + timedelta(minutes=5))
    assert store.pop_session(5) == '{"a":1}'
    assert store.pop_session(5) is None
    assert store.get_session(5, now) is None


def test_pop_session_missing_returns_none(store):
    assert store.pop_session(999) is None


def test_pop_expired_session_deletes_when_still_expired(store):
    now = datetime.now(UTC)
    store.save_session(1, "stale", now - timedelta(seconds=1))
    assert store.pop_expired_session(1, now) == "stale"
    assert store.get_session(1, now - timedelta(minutes=5)) is None


def test_pop_expired_session_does_not_delete_a_renewal_past_now(store):
    """The TOCTOU guard: a row that looked expired at scan time but was renewed with a later
    expires_at before the delete must survive, with its renewed payload intact."""
    now = datetime.now(UTC)
    store.save_session(1, "stale", now - timedelta(seconds=1))
    # Simulate a concurrent renewal landing after the scan but before the delete.
    store.save_session(1, "renewed", now + timedelta(minutes=5))
    assert store.pop_expired_session(1, now) is None
    assert store.get_session(1, now) == "renewed"


def test_pop_expired_session_missing_returns_none(store):
    now = datetime.now(UTC)
    assert store.pop_expired_session(999, now) is None


def test_expired_sessions_and_pop_session_thread_safe(store):
    now = datetime.now(UTC)
    for cid in range(20):
        store.save_session(cid, f"payload-{cid}", now - timedelta(seconds=1))
    errors: list[Exception] = []

    def worker(cid: int) -> None:
        try:
            store.expired_sessions(now)
            store.pop_session(cid)
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(cid,)) for cid in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert store.expired_sessions(now) == []


def test_execution_undo_window(store):
    now = datetime.now(UTC)
    eid = store.new_event(telegram_user_id=1, chat_id=9, kind="text")
    xid = store.add_execution(eid, 9, 100, '{"kind":"archive","page_id":"p"}',
                              now + timedelta(minutes=5))
    assert store.get_execution(xid, now)["undo"] == '{"kind":"archive","page_id":"p"}'
    assert store.latest_execution(9, now)["id"] == xid
    assert store.get_execution(xid, now + timedelta(minutes=6)) is None
    store.mark_undone(xid)
    assert store.get_execution(xid, now)["undone"] == 1
    assert store.latest_execution(9, now) is None


def test_reply_message_id_is_set_after_the_reply_is_sent(store):
    """The row is written while the command runs, before its reply exists, so the column starts
    null and the transport fills it in afterwards — it is how the undo button gets edited away
    when the window closes."""
    now = datetime.now(UTC)
    eid = store.new_event(telegram_user_id=1, chat_id=9, kind="text")
    xid = store.add_execution(eid, 9, None, "{}", now + timedelta(minutes=5))
    assert store.get_execution(xid, now)["reply_message_id"] is None

    store.set_reply_message_id(xid, 4242)
    assert store.get_execution(xid, now)["reply_message_id"] == 4242
    # and it addresses one row: a second execution in the same chat is untouched
    other = store.add_execution(eid, 9, None, "{}", now + timedelta(minutes=5))
    assert store.get_execution(other, now)["reply_message_id"] is None


def test_migrate_idempotent(tmp_path):
    p = tmp_path / "t.sqlite"
    AuditStore(p).migrate()
    s = AuditStore(p)
    s.migrate()
    s.close()


def test_execution_requires_existing_event(store):
    now = datetime.now(UTC)
    with pytest.raises(sqlite3.IntegrityError):
        store.add_execution(999, 9, None, "{}", now + timedelta(minutes=5))


def test_update_event_unknown_column_rejected(store):
    eid = store.new_event(telegram_user_id=1, chat_id=1, kind="text")
    with pytest.raises(ValueError):
        store.update_event(eid, notion_token="x")
