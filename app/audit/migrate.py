"""SQLite migration runner.

Migration files must not manage transactions (BEGIN/COMMIT/ROLLBACK/END) or run VACUUM —
each migration is wrapped in its own transaction by the runner, and a migration that manages
its own transaction defeats that atomicity guarantee.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from app.version import app_root

MIGRATIONS_DIR = app_root() / "migrations"
_NAME = re.compile(r"^(\d{4})_([A-Za-z0-9_]+)\.sql$")
JOURNAL = (
    "CREATE TABLE IF NOT EXISTS schema_migrations ("
    "version INTEGER PRIMARY KEY, name TEXT NOT NULL, checksum TEXT NOT NULL, "
    "applied_at TEXT NOT NULL)"
)
_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.DOTALL)
_TXN_TOKEN = re.compile(r"\b(BEGIN|COMMIT|ROLLBACK|VACUUM)\b", re.IGNORECASE)
_END_TOKEN = re.compile(r"\bEND\s+TRANSACTION\b|\bEND\s*;", re.IGNORECASE)


def _check_no_transaction_control(sql: str, filename: str) -> None:
    stripped = _COMMENT.sub(" ", sql)
    m = _TXN_TOKEN.search(stripped)
    token = m.group(1).upper() if m else None
    if not token and _END_TOKEN.search(stripped):
        token = "END"
    if token:
        raise MigrationError(
            f"migration {filename} must not contain transaction control or VACUUM: {token}"
        )


class MigrationError(Exception):
    pass


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str
    checksum: str


@dataclass
class Report:
    applied: list[Migration] = field(default_factory=list)
    pending: list[Migration] = field(default_factory=list)
    backup_path: Path | None = None


def _now() -> datetime:
    return datetime.now(UTC)


def discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]:
    found: dict[int, Migration] = {}
    for p in sorted(directory.glob("*.sql")):
        m = _NAME.match(p.name)
        if not m:
            continue
        version, name = int(m.group(1)), m.group(2)
        if version in found:
            raise MigrationError(f"duplicate migration version {version}: {p.name}")
        sql = p.read_text(encoding="utf-8")
        _check_no_transaction_control(sql, p.name)
        found[version] = Migration(version, name, p, sql, hashlib.sha256(sql.encode()).hexdigest())
    return [found[v] for v in sorted(found)]


def applied(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    conn.execute(JOURNAL)
    rows = conn.execute("SELECT version, name, checksum FROM schema_migrations").fetchall()
    return {int(v): (n, c) for v, n, c in rows}


def pending(conn: sqlite3.Connection, migrations: list[Migration]) -> list[Migration]:
    done = applied(conn)
    out = []
    for m in migrations:
        if m.version in done:
            if done[m.version][1] != m.checksum:
                raise MigrationError(
                    f"migration {m.version:04d} checksum mismatch: "
                    "file changed after it was applied"
                )
            continue
        out.append(m)
    return out


def status(db_path: Path, directory: Path = MIGRATIONS_DIR) -> Report:
    migrations = discover(directory)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        return Report(pending=pending(conn, migrations))


def apply(
    db_path: Path,
    directory: Path = MIGRATIONS_DIR,
    *,
    dry_run: bool = False,
    backup: bool = True,
    now: Callable[[], datetime] = _now,
) -> Report:
    migrations = discover(directory)
    existed = db_path.exists() and db_path.stat().st_size > 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None)
    try:
        todo = pending(conn, migrations)
        report = Report(pending=list(todo))
        if dry_run or not todo:
            return report
        if backup and existed:
            stamp = now().strftime("%Y%m%dT%H%M%SZ")
            report.backup_path = db_path.with_name(
                f"{db_path.stem}.pre-{todo[0].version:04d}-{stamp}{db_path.suffix}"
            )
            shutil.copy2(db_path, report.backup_path)
        for m in todo:
            script = (
                "BEGIN;\n"
                f"{m.sql}\n"
                "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                f"VALUES ({m.version}, '{m.name}', '{m.checksum}', '{now().isoformat()}');\n"
                "COMMIT;"
            )
            try:
                conn.executescript(script)
            except sqlite3.Error as e:
                conn.execute("ROLLBACK") if conn.in_transaction else None
                raise MigrationError(f"migration {m.version:04d}_{m.name} failed: {e}") from e
            report.applied.append(m)
        report.pending = []
        return report
    finally:
        conn.close()
