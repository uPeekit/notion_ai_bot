import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.audit.migrate import MIGRATIONS_DIR, MigrationError, apply, discover, status


def write(d: Path, name: str, sql: str) -> Path:
    p = d / name
    p.write_text(sql, encoding="utf-8")
    return p


@pytest.fixture
def mig(tmp_path):
    d = tmp_path / "migrations"
    d.mkdir()
    write(d, "0001_init.sql", "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT);")
    write(d, "0002_add_b.sql", "ALTER TABLE t ADD COLUMN b TEXT;")
    return d


def test_discover_sorted_and_parsed(mig):
    ms = discover(mig)
    assert [(m.version, m.name) for m in ms] == [(1, "init"), (2, "add_b")]
    assert len(ms[0].checksum) == 64


def test_discover_duplicate_version_raises(mig):
    write(mig, "0002_dup.sql", "SELECT 1;")
    with pytest.raises(MigrationError):
        discover(mig)


def test_discover_ignores_non_matching_files(mig):
    write(mig, "README.md", "x")
    write(mig, "notes.sql", "SELECT 1;")
    assert len(discover(mig)) == 2


def test_apply_all_then_noop(tmp_path, mig):
    db = tmp_path / "x.sqlite"
    r = apply(db, mig, backup=False)
    assert [m.version for m in r.applied] == [1, 2]
    with sqlite3.connect(db) as c:
        cols = [row[1] for row in c.execute("PRAGMA table_info(t)")]
        journal = c.execute(
            "SELECT version, name FROM schema_migrations ORDER BY version"
        ).fetchall()
    assert cols == ["id", "a", "b"]
    assert journal == [(1, "init"), (2, "add_b")]
    r2 = apply(db, mig, backup=False)
    assert r2.applied == [] and r2.pending == []


def test_apply_only_pending_and_backup(tmp_path, mig):
    db = tmp_path / "x.sqlite"
    apply(db, mig, backup=False)
    write(mig, "0003_c.sql", "ALTER TABLE t ADD COLUMN c TEXT;")
    fixed = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
    r = apply(db, mig, now=lambda: fixed)
    assert [m.version for m in r.applied] == [3]
    assert r.backup_path == tmp_path / "x.pre-0003-20260909T120000Z.sqlite"
    assert r.backup_path.exists()


def test_no_backup_for_fresh_db(tmp_path, mig):
    r = apply(tmp_path / "new.sqlite", mig)
    assert r.backup_path is None


def test_dry_run_changes_nothing(tmp_path, mig):
    db = tmp_path / "x.sqlite"
    r = apply(db, mig, dry_run=True)
    assert [m.version for m in r.pending] == [1, 2] and r.applied == []
    assert not db.exists() or sqlite3.connect(db).execute(
        "SELECT count(*) FROM sqlite_master WHERE name='t'").fetchone()[0] == 0


def test_checksum_mismatch_raises(tmp_path, mig):
    db = tmp_path / "x.sqlite"
    apply(db, mig, backup=False)
    write(mig, "0001_init.sql", "CREATE TABLE t (id INTEGER PRIMARY KEY, a TEXT, z TEXT);")
    with pytest.raises(MigrationError, match="checksum"):
        status(db, mig)


def test_failed_migration_is_atomic(tmp_path, mig):
    db = tmp_path / "x.sqlite"
    apply(db, mig, backup=False)
    write(
        mig, "0003_bad.sql",
        "ALTER TABLE t ADD COLUMN d TEXT; ALTER TABLE nope ADD COLUMN e TEXT;",
    )
    with pytest.raises(MigrationError):
        apply(db, mig, backup=False)
    with sqlite3.connect(db) as c:
        cols = [row[1] for row in c.execute("PRAGMA table_info(t)")]
        assert "d" not in cols
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2


@pytest.mark.parametrize("sql", [
    "BEGIN; CREATE TABLE q (id INTEGER); COMMIT;",
    "CREATE TABLE q (id INTEGER); VACUUM;",
    "create table q (id integer);\ncommit;",
])
def test_discover_rejects_transaction_control(mig, sql):
    write(mig, "0003_bad.sql", sql)
    with pytest.raises(MigrationError, match="transaction control"):
        discover(mig)


def test_discover_allows_tokens_in_comments_and_identifiers(mig):
    write(
        mig, "0003_ok.sql",
        "-- we do not BEGIN here\n"
        "CREATE TABLE commit_log (id INTEGER, begin_at TEXT);",
    )
    assert [m.version for m in discover(mig)] == [1, 2, 3]


def test_real_initial_migration_matches_store(tmp_path):
    db = tmp_path / "bot.sqlite"
    r = apply(db, MIGRATIONS_DIR, backup=False)
    assert [m.version for m in r.applied] == [1]
    with sqlite3.connect(db) as c:
        names = {row[0] for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"events", "sessions", "executions", "schema_migrations"} <= names


def test_store_assert_schema_current(tmp_path):
    from app.audit.store import AuditStore

    s = AuditStore(tmp_path / "bot.sqlite")
    with pytest.raises(MigrationError):
        s.assert_schema_current()
    s.migrate()
    s.assert_schema_current()
    s.close()
