import sqlite3
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
