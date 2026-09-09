# Plan 1b: Versioning, Releases, SQLite Migrations, Prod Updates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the bot as versioned zip releases (full / patch) installable into a separate prod directory, with numbered SQLite migrations applied by the update script, and a rollback path.

**Architecture:** `pyproject.toml` version is the source of truth (`0.0.1`). `release.py` bumps, tags, tests, and zips the app layer plus `manifest.json`. The prod directory has its own `.venv` created by `uv sync --frozen --no-dev`; `apply_update.py` (stdlib only) backs up, extracts, re-syncs the venv when `uv.lock` changed, runs migrations explicitly, writes `VERSION`. Migrations are numbered `.sql` files under `migrations/`, journaled in `schema_migrations` with checksums, applied only by the migrate CLI (never lazily at startup; startup only verifies).

**Tech Stack:** Python 3.12 stdlib (`tomllib`, `zipfile`, `hashlib`, `sqlite3`, `subprocess`), uv, PowerShell wrappers.

**Spec:** Design agreed in chat on 2026-09-09 (this file records it). Adapted from `C:\coding\scalper_bot` (`release.py`, `apply_update.py`, `src/persistence/migration_runner.py`), simplified: no embedded runtime, explicit migration step, pid-file running check.

## Global Constraints

- Version bump: `--patch` → 0.0.x+1, `--full` → 0.x+1.0, `--auto` → full if `uv.lock` changed since last tag, patch if app files changed, none otherwise. Tags `vX.Y.Z`.
- Zip layout is flat (files at zip root) so extraction over the install root works. Never ship `.env`, `data/`, `logs/`, `.venv/`, `dist/`, `tests/`, `docs/`, `.superpowers/`.
- Protected paths on the prod side, never overwritten or deleted by updates: `.env`, `data/`, `logs/`, `.venv/`, `.backup/`, `VERSION` is rewritten only at the end of a successful update.
- Migrations: forward-only, immutable once applied (checksum mismatch is an error), each applied atomically with its journal row, DB backed up to `<db>.pre-<version>-<timestamp>.sqlite` before applying.
- Startup never applies migrations; it raises if any are pending.
- Secrets never printed by any script.
- `uv run ruff check .` and `uv run pytest -q` pass before each commit; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; named `git add` only.
- Shell is PowerShell; uv may be at `$env:USERPROFILE\.local\bin\uv.exe`.

## File structure

```text
pyproject.toml                 version = "0.0.1"
app/version.py                 get_version(), app_root()
app/audit/migrate.py           Migration discovery/journal/apply (library)
app/audit/store.py             migrate() delegates to runner; assert_schema_current()
migrations/0001_initial.sql    events/sessions/executions (moved from store.py)
tools/migrate.py               CLI: --status | --dry-run | --apply, --db
release.py                     repo root; build dist/notion_ai_bot-X.Y.Z.zip
apply_update.py                shipped; prod-side updater with --rollback
deploy/install.ps1             first install into a target dir
deploy/update.ps1              wrapper: .venv python apply_update.py <zip>
deploy/run.ps1                 wrapper: .venv python -m app.main
RELEASE.md                     how to release / install / update / roll back
tests/test_version.py, tests/test_migrate.py, tests/test_release.py, tests/test_apply_update.py
```

---

### Task 1: Version source and `app/version.py`

**Files:**
- Modify: `pyproject.toml` (`version = "0.0.1"`)
- Create: `app/version.py`, `tests/test_version.py`

**Interfaces:**
- Produces: `app_root() -> Path` (directory containing `app/`), `get_version() -> str` (VERSION file → pyproject → `"dev"`), `read_pyproject_version(path: Path) -> str`.

- [ ] **Step 1: Failing tests** — `tests/test_version.py`:

```python
from pathlib import Path

from app.version import app_root, get_version, read_pyproject_version


def test_app_root_contains_app_package():
    assert (app_root() / "app" / "__init__.py").exists()


def test_pyproject_version_is_0_0_1():
    assert read_pyproject_version(app_root() / "pyproject.toml") == "0.0.1"


def test_version_prefers_version_file(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr("app.version.app_root", lambda: tmp_path)
    assert get_version() == "9.9.9"


def test_version_falls_back_to_pyproject_then_dev(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\nversion="1.2.3"\n', encoding="utf-8")
    monkeypatch.setattr("app.version.app_root", lambda: tmp_path)
    assert get_version() == "1.2.3"
    (tmp_path / "pyproject.toml").unlink()
    assert get_version() == "dev"


def test_read_pyproject_version_missing_raises(tmp_path):
    (tmp_path / "pyproject.toml").write_text('[project]\nname="x"\n', encoding="utf-8")
    try:
        read_pyproject_version(tmp_path / "pyproject.toml")
    except KeyError:
        return
    raise AssertionError("expected KeyError")
```

- [ ] **Step 2: Run** `uv run pytest tests/test_version.py -q` → ImportError.

- [ ] **Step 3: Implement** — set `version = "0.0.1"` in `pyproject.toml`; `app/version.py`:

```python
from __future__ import annotations

import tomllib
from pathlib import Path


def app_root() -> Path:
    return Path(__file__).resolve().parents[1]


def read_pyproject_version(path: Path) -> str:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return str(data["project"]["version"])


def get_version() -> str:
    root = app_root()
    vf = root / "VERSION"
    if vf.exists():
        return vf.read_text(encoding="utf-8").strip()
    pp = root / "pyproject.toml"
    if pp.exists():
        try:
            return read_pyproject_version(pp)
        except (KeyError, tomllib.TOMLDecodeError):
            pass
    return "dev"
```

- [ ] **Step 4: Run** tests → 5 passed. `uv run ruff check .`.
- [ ] **Step 5: Commit** `feat: version source (pyproject 0.0.1, VERSION file override)`.

---

### Task 2: Migration runner, `0001_initial.sql`, store integration, CLI

**Files:**
- Create: `migrations/0001_initial.sql`, `app/audit/migrate.py`, `tools/migrate.py`, `tests/test_migrate.py`
- Modify: `app/audit/store.py` (remove inline `SCHEMA`; `migrate()` delegates; add `assert_schema_current()`), `tests/test_audit_store.py` (fixture unchanged: `store.migrate()` still works)

**Interfaces:**
- Produces in `app/audit/migrate.py`:
  - `MIGRATIONS_DIR: Path` = `app_root() / "migrations"`
  - `@dataclass(frozen=True) Migration(version: int, name: str, path: Path, sql: str, checksum: str)`
  - `class MigrationError(Exception)`
  - `discover(directory: Path = MIGRATIONS_DIR) -> list[Migration]` sorted by version; duplicate version → `MigrationError`; file name pattern `NNNN_name.sql`.
  - `applied(conn) -> dict[int, tuple[str, str]]` (version → (name, checksum)); creates journal table if missing.
  - `pending(conn, migrations) -> list[Migration]`; checksum mismatch on an applied version → `MigrationError`.
  - `apply(db_path: Path, directory: Path = MIGRATIONS_DIR, *, dry_run: bool = False, backup: bool = True, now: Callable[[], datetime] = ...) -> Report` where `@dataclass Report(applied: list[Migration], pending: list[Migration], backup_path: Path | None)`; dry-run applies nothing.
  - `status(db_path, directory) -> Report` (no changes).
- `AuditStore.migrate()` → `apply(self._path)`; `AuditStore.assert_schema_current()` raises `MigrationError` listing pending versions.
- `tools/migrate.py`: `python -m tools.migrate --status|--dry-run|--apply [--db PATH] [--dir PATH]`; without `--db` loads `Settings().db_path`; exit 0 ok, 1 error, 2 pending (for `--status`).

- [ ] **Step 1: Write `migrations/0001_initial.sql`** — the three `CREATE TABLE IF NOT EXISTS` statements currently in `app/audit/store.py::SCHEMA`, verbatim, without `BEGIN`/`COMMIT`. Header comment: `-- 0001 initial schema: events, sessions, executions`.

- [ ] **Step 2: Failing tests** — `tests/test_migrate.py`:

```python
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
        journal = c.execute("SELECT version, name FROM schema_migrations ORDER BY version").fetchall()
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
    write(mig, "0003_bad.sql", "ALTER TABLE t ADD COLUMN d TEXT; ALTER TABLE nope ADD COLUMN e TEXT;")
    with pytest.raises(MigrationError):
        apply(db, mig, backup=False)
    with sqlite3.connect(db) as c:
        cols = [row[1] for row in c.execute("PRAGMA table_info(t)")]
        assert "d" not in cols
        assert c.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2


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
```

- [ ] **Step 3: Run** → ImportError.

- [ ] **Step 4: Implement `app/audit/migrate.py`**

```python
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
                    f"migration {m.version:04d} checksum mismatch: file changed after it was applied"
                )
            continue
        out.append(m)
    return out


def status(db_path: Path, directory: Path = MIGRATIONS_DIR) -> Report:
    migrations = discover(directory)
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
```

Note: `m.name` matches `[A-Za-z0-9_]+`, so the inline `VALUES` cannot inject; keep the regex strict.

- [ ] **Step 5: Update `app/audit/store.py`** — delete `SCHEMA`; keep `EVENT_COLUMNS`; store `self._path = path`; replace `migrate()` body with `apply(self._path)` (import from `app.audit.migrate`; note `apply` needs the file path, not the connection — call it before other operations; the existing connection sees new tables because SQLite reads schema per statement); add:

```python
    def assert_schema_current(self) -> None:
        with self._lock:
            todo = pending(self._conn, discover())
        if todo:
            versions = ", ".join(f"{m.version:04d}" for m in todo)
            raise MigrationError(f"database schema out of date; pending migrations: {versions}. "
                                 "Run: python -m tools.migrate --apply")
```

- [ ] **Step 6: `tools/migrate.py`**

```python
"""Apply or inspect SQLite migrations. Never runs implicitly at bot startup."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.audit.migrate import MIGRATIONS_DIR, MigrationError, apply, status


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tools.migrate")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--status", action="store_true")
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--apply", action="store_true")
    ap.add_argument("--db", type=Path, help="sqlite path (default: DB_PATH from .env)")
    ap.add_argument("--dir", type=Path, default=MIGRATIONS_DIR)
    a = ap.parse_args(argv)

    db = a.db
    if db is None:
        from app.config import load_settings

        db = load_settings().db_path
    try:
        if a.apply:
            r = apply(db, a.dir)
            for m in r.applied:
                print(f"applied {m.version:04d}_{m.name}")
            if r.backup_path:
                print(f"backup  {r.backup_path}")
            print(f"{len(r.applied)} migration(s) applied; schema current")
            return 0
        r = status(db, a.dir) if a.status else apply(db, a.dir, dry_run=True)
        if not r.pending:
            print("schema current")
            return 0
        for m in r.pending:
            print(f"pending {m.version:04d}_{m.name}")
        return 2
    except MigrationError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 7: Run** `uv run pytest -q` (all, including `tests/test_audit_store.py`) and ruff. Also `uv run python -m tools.migrate --status --db data/tmp.sqlite` → prints `pending 0001_initial`, exit 2; delete `data/tmp.sqlite`.
- [ ] **Step 8: Commit** `feat: sqlite migrations runner, 0001_initial, migrate CLI`.

---

### Task 3: `release.py`

**Files:**
- Create: `release.py` (repo root), `tests/test_release.py`
- Modify: `.gitignore` (add `dist/`, `VERSION`, `.backup/`)

**Interfaces:**
- `release.py` functions (importable for tests): `bump(version: str, kind: Literal["patch","full"]) -> str`; `classify_changes(changed: list[str]) -> Literal["full","patch","none"]`; `collect_files(root: Path) -> list[Path]` (relative, sorted); `build_zip(root: Path, version: str, kind: str, out_dir: Path) -> Path`; `write_manifest(...)`; `main(argv) -> int`.
- Manifest schema: `{"name": "notion_ai_bot", "version", "kind", "lock_hash", "built_at", "files": [...]}`.
- Include list: `app/**/*.py`, `tools/**/*.py`, `migrations/*.sql`, `deploy/*.ps1`, `apply_update.py`, `pyproject.toml`, `uv.lock`, `.env.example`, `README.md`, `RELEASE.md`, `documentation/NOTION_SETUP.md`. Exclude `__pycache__`.
- CLI: `uv run python release.py (--patch | --full | --auto | --set-version X.Y.Z) [--allow-dirty] [--skip-tests] [--no-tag] [--output dist] [--dry-run]`.

- [ ] **Step 1: Failing tests** — `tests/test_release.py`:

```python
import json
import zipfile
from pathlib import Path

import pytest

import release


def test_bump():
    assert release.bump("0.0.1", "patch") == "0.0.2"
    assert release.bump("0.0.9", "full") == "0.1.0"
    assert release.bump("1.4.7", "full") == "1.5.0"
    with pytest.raises(ValueError):
        release.bump("1.2", "patch")


def test_classify_changes():
    assert release.classify_changes(["uv.lock", "app/x.py"]) == "full"
    assert release.classify_changes(["app/x.py"]) == "patch"
    assert release.classify_changes(["migrations/0002_a.sql"]) == "patch"
    assert release.classify_changes(["deploy/run.ps1", "apply_update.py"]) == "patch"
    assert release.classify_changes(["docs/a.md", "tests/test_x.py", "README.md"]) == "none"
    assert release.classify_changes([]) == "none"


@pytest.fixture
def fake_repo(tmp_path):
    for rel, text in {
        "pyproject.toml": '[project]\nname="notion-ai-bot"\nversion="0.0.1"\n',
        "uv.lock": "lock",
        ".env.example": "X=",
        "README.md": "r",
        "RELEASE.md": "rel",
        "apply_update.py": "print(1)",
        "app/__init__.py": "",
        "app/a.py": "A=1",
        "app/__pycache__/a.cpython-312.pyc": "junk",
        "tools/__init__.py": "",
        "migrations/0001_initial.sql": "CREATE TABLE x (id INTEGER);",
        "deploy/run.ps1": "echo run",
        "documentation/NOTION_SETUP.md": "n",
        "tests/test_a.py": "def test(): pass",
        ".env": "SECRET=1",
        "data/bot.sqlite": "db",
    }.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


def test_collect_files_includes_only_shippable(fake_repo):
    files = {str(p).replace("\\", "/") for p in release.collect_files(fake_repo)}
    assert "app/a.py" in files and "migrations/0001_initial.sql" in files
    assert "deploy/run.ps1" in files and "apply_update.py" in files and "uv.lock" in files
    assert not any(f.startswith(("tests/", "data/")) for f in files)
    assert ".env" not in files and "app/__pycache__/a.cpython-312.pyc" not in files


def test_build_zip_layout_and_manifest(fake_repo):
    out = fake_repo / "dist"
    z = release.build_zip(fake_repo, "0.0.2", "patch", out)
    assert z == out / "notion_ai_bot-0.0.2.zip"
    with zipfile.ZipFile(z) as zf:
        names = set(zf.namelist())
        assert {"VERSION", "manifest.json", "app/a.py", "uv.lock"} <= names
        assert zf.read("VERSION").decode().strip() == "0.0.2"
        man = json.loads(zf.read("manifest.json"))
    assert man["name"] == "notion_ai_bot" and man["version"] == "0.0.2" and man["kind"] == "patch"
    assert man["lock_hash"] == release.sha256_file(fake_repo / "uv.lock")
    assert "app/a.py" in man["files"] and ".env" not in man["files"]
    assert not any(n.startswith("/") or ".." in n for n in names)


def test_set_pyproject_version(fake_repo):
    release.set_pyproject_version(fake_repo / "pyproject.toml", "0.0.5")
    assert 'version="0.0.5"' in (fake_repo / "pyproject.toml").read_text(encoding="utf-8") or \
        'version = "0.0.5"' in (fake_repo / "pyproject.toml").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement `release.py`**

```python
"""Build a versioned release zip. Usage: uv run python release.py --patch|--full|--auto"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

NAME = "notion_ai_bot"
ROOT = Path(__file__).resolve().parent
INCLUDE_GLOBS = [
    "app/**/*.py", "tools/**/*.py", "migrations/*.sql", "deploy/*.ps1",
    "apply_update.py", "pyproject.toml", "uv.lock", ".env.example", "README.md", "RELEASE.md",
    "documentation/NOTION_SETUP.md",
]
APP_PREFIXES = ("app/", "tools/", "migrations/", "deploy/", "apply_update.py", "pyproject.toml")
Kind = Literal["patch", "full"]


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def bump(version: str, kind: Kind) -> str:
    parts = version.split(".")
    if len(parts) != 3 or not all(x.isdigit() for x in parts):
        raise ValueError(f"bad version {version!r}")
    major, minor, patch = (int(x) for x in parts)
    return f"{major}.{minor + 1}.0" if kind == "full" else f"{major}.{minor}.{patch + 1}"


def classify_changes(changed: list[str]) -> Literal["full", "patch", "none"]:
    norm = [c.replace("\\", "/") for c in changed]
    if "uv.lock" in norm:
        return "full"
    if any(c.startswith(APP_PREFIXES) for c in norm):
        return "patch"
    return "none"


def collect_files(root: Path) -> list[Path]:
    out: set[Path] = set()
    for g in INCLUDE_GLOBS:
        for p in root.glob(g):
            if p.is_file() and "__pycache__" not in p.parts:
                out.add(p.relative_to(root))
    return sorted(out, key=lambda p: p.as_posix())


def set_pyproject_version(path: Path, version: str) -> None:
    text = path.read_text(encoding="utf-8")
    new, n = re.subn(r'(?m)^version\s*=\s*"[^"]*"', f'version = "{version}"', text, count=1)
    if n != 1:
        raise ValueError("version line not found in pyproject.toml")
    path.write_text(new, encoding="utf-8")


def build_zip(root: Path, version: str, kind: str, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = collect_files(root)
    manifest = {
        "name": NAME, "version": version, "kind": kind,
        "lock_hash": sha256_file(root / "uv.lock"),
        "built_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": [f.as_posix() for f in files] + ["VERSION", "manifest.json"],
    }
    zpath = out_dir / f"{NAME}-{version}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(root / f, f.as_posix())
        zf.writestr("VERSION", version + "\n")
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
    return zpath


def git(*args: str) -> str:
    return subprocess.run(["git", *args], cwd=ROOT, check=True, capture_output=True,
                          text=True).stdout.strip()


def last_tag() -> str | None:
    tags = git("tag", "--list", "v*", "--sort=-v:refname").splitlines()
    return tags[0] if tags else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--patch", action="store_true")
    g.add_argument("--full", action="store_true")
    g.add_argument("--auto", action="store_true")
    g.add_argument("--set-version")
    ap.add_argument("--allow-dirty", action="store_true")
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--no-tag", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--output", type=Path, default=ROOT / "dist")
    a = ap.parse_args(argv)

    if git("status", "--porcelain") and not a.allow_dirty:
        print("working tree is dirty; commit first or pass --allow-dirty", file=sys.stderr)
        return 1

    from app.version import read_pyproject_version

    current = read_pyproject_version(ROOT / "pyproject.toml")
    tag = last_tag()
    if a.set_version:
        version, kind = a.set_version, "full"
    elif a.auto:
        changed = git("diff", "--name-only", f"{tag}..HEAD").splitlines() if tag else ["uv.lock"]
        kind = classify_changes(changed)
        if kind == "none":
            print("no app changes since", tag)
            return 0
        version = bump(current, kind)
    else:
        kind = "full" if a.full else "patch"
        version = bump(current, kind)

    print(f"release {current} -> {version} ({kind}); last tag {tag}")
    if a.dry_run:
        return 0
    if not a.skip_tests:
        subprocess.run(["uv", "run", "pytest", "-q"], cwd=ROOT, check=True)
        subprocess.run(["uv", "run", "ruff", "check", "."], cwd=ROOT, check=True)

    set_pyproject_version(ROOT / "pyproject.toml", version)
    subprocess.run(["uv", "lock", "--offline"], cwd=ROOT, check=False, capture_output=True)
    git("add", "pyproject.toml", "uv.lock")
    git("commit", "-m", f"release: v{version}")
    if not a.no_tag:
        git("tag", "-a", f"v{version}", "-m", f"v{version}")

    z = build_zip(ROOT, version, kind, a.output)
    (a.output / "last_release.json").write_text(
        json.dumps({"version": version, "kind": kind, "zip": z.name}, indent=2), encoding="utf-8"
    )
    print(f"built {z}")
    print(f"next: git push && git push --tags; install with deploy\\update.ps1 {z}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

`uv lock --offline` refreshes the lock's own `version` entry for this package after the bump; if `uv` is not on PATH, resolve it via `shutil.which("uv") or Path.home()/".local/bin/uv.exe"` for both this call and the test run (add a helper `uv_exe()`).

- [ ] **Step 4:** `.gitignore` += `dist/`, `VERSION`, `.backup/`. Run tests + ruff; `uv run python release.py --auto --dry-run` prints a plan line without changing anything.
- [ ] **Step 5: Commit** `feat: release script (bump, tag, zip with manifest)`.

---

### Task 4: `apply_update.py` and deploy wrappers

**Files:**
- Create: `apply_update.py`, `deploy/install.ps1`, `deploy/update.ps1`, `deploy/run.ps1`, `tests/test_apply_update.py`

**Interfaces:**
- `apply_update.py` (stdlib only; must run with the prod `.venv` python or any 3.12): functions `read_manifest(zip_path) -> dict`, `is_newer(new: str, current: str) -> bool`, `current_version(root) -> str` (`VERSION` file or `"0.0.0"`), `bot_running(root) -> bool` (reads `data/bot.pid`; Windows: `tasklist /FI "PID eq N"` contains the pid; POSIX: `os.kill(pid, 0)`), `backup_app_layer(root, version) -> Path` (copies every path listed in the installed `manifest.json` `files` to `.backup/<version>/`; keeps newest 2 backups), `extract(zip_path, root) -> int` (skips `PROTECTED`), `prune_removed(root, old_files, new_files) -> list[str]`, `sync_runtime(root)` (`uv sync --frozen --no-dev`), `run_migrations(root)` (`.venv/Scripts/python.exe -m tools.migrate --apply`, POSIX `.venv/bin/python`), `apply(zip_path, root, *, force=False, dry_run=False) -> int`, `rollback(root) -> int`, `main(argv)`.
- `PROTECTED = {".env", "data", "logs", ".venv", ".backup"}`; `VERSION` written last.
- Exit codes: 0 ok, 1 refused (running / not newer / bad archive), 2 step failed (after backup; message names `--rollback`).

- [ ] **Step 1: Failing tests** — `tests/test_apply_update.py` (build zips with `release.build_zip` from a fake repo; monkeypatch `sync_runtime`, `run_migrations`, `bot_running`):

```python
import json
import zipfile
from pathlib import Path

import pytest

import apply_update as au
import release


def make_repo(root: Path, version: str, extra: dict[str, str] | None = None) -> Path:
    files = {
        "pyproject.toml": f'[project]\nname="notion-ai-bot"\nversion="{version}"\n',
        "uv.lock": "lock-v1", ".env.example": "X=", "README.md": "r", "RELEASE.md": "rel",
        "apply_update.py": "print(1)", "app/__init__.py": "", "app/a.py": f"V='{version}'",
        "tools/__init__.py": "", "migrations/0001_initial.sql": "CREATE TABLE x (id INTEGER);",
        "deploy/run.ps1": "echo run", "documentation/NOTION_SETUP.md": "n",
    }
    files.update(extra or {})
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture
def calls(monkeypatch):
    log = []
    monkeypatch.setattr(au, "sync_runtime", lambda root: log.append("sync"))
    monkeypatch.setattr(au, "run_migrations", lambda root: log.append("migrate"))
    monkeypatch.setattr(au, "bot_running", lambda root: False)
    return log


def install(tmp_path, version="0.0.1", extra=None) -> tuple[Path, Path]:
    repo = make_repo(tmp_path / f"repo-{version}", version, extra)
    z = release.build_zip(repo, version, "full", tmp_path / "dist")
    prod = tmp_path / "prod"
    prod.mkdir()
    with zipfile.ZipFile(z) as zf:
        zf.extractall(prod)
    (prod / ".env").write_text("SECRET=1", encoding="utf-8")
    (prod / "data").mkdir()
    (prod / "data" / "bot.sqlite").write_text("db", encoding="utf-8")
    return prod, z


def test_is_newer():
    assert au.is_newer("0.0.2", "0.0.1") and au.is_newer("0.1.0", "0.0.9")
    assert not au.is_newer("0.0.1", "0.0.1") and not au.is_newer("0.0.1", "0.0.2")
    assert au.is_newer("0.10.0", "0.9.0")


def test_patch_update_happy_path(tmp_path, calls):
    prod, _ = install(tmp_path)
    repo2 = make_repo(tmp_path / "repo-0.0.2", "0.0.2", {"app/b.py": "B=1"})
    (repo2 / "app" / "a.py").write_text("V='0.0.2'", encoding="utf-8")
    z2 = release.build_zip(repo2, "0.0.2", "patch", tmp_path / "dist")
    assert au.apply(z2, prod) == 0
    assert (prod / "VERSION").read_text().strip() == "0.0.2"
    assert (prod / "app" / "b.py").exists()
    assert (prod / "app" / "a.py").read_text() == "V='0.0.2'"
    assert (prod / ".env").read_text() == "SECRET=1"
    assert (prod / "data" / "bot.sqlite").read_text() == "db"
    assert calls == ["migrate"]  # same lock hash → no sync
    assert (prod / ".backup" / "0.0.1" / "app" / "a.py").read_text() == "V='0.0.1'"


def test_full_update_syncs_runtime_when_lock_changed(tmp_path, calls):
    prod, _ = install(tmp_path)
    repo2 = make_repo(tmp_path / "repo-0.1.0", "0.1.0", {"uv.lock": "lock-v2"})
    z2 = release.build_zip(repo2, "0.1.0", "full", tmp_path / "dist")
    assert au.apply(z2, prod) == 0
    assert calls == ["sync", "migrate"]


def test_removed_files_are_pruned(tmp_path, calls):
    prod, _ = install(tmp_path, extra={"app/old.py": "OLD=1"})
    repo2 = make_repo(tmp_path / "repo-0.0.2", "0.0.2")
    z2 = release.build_zip(repo2, "0.0.2", "patch", tmp_path / "dist")
    assert (prod / "app" / "old.py").exists()
    assert au.apply(z2, prod) == 0
    assert not (prod / "app" / "old.py").exists()


def test_refuses_older_or_equal_unless_force(tmp_path, calls):
    prod, z1 = install(tmp_path)
    assert au.apply(z1, prod) == 1
    assert au.apply(z1, prod, force=True) == 0


def test_refuses_when_running(tmp_path, calls, monkeypatch):
    prod, _ = install(tmp_path)
    monkeypatch.setattr(au, "bot_running", lambda root: True)
    repo2 = make_repo(tmp_path / "repo-0.0.2", "0.0.2")
    z2 = release.build_zip(repo2, "0.0.2", "patch", tmp_path / "dist")
    assert au.apply(z2, prod) == 1


def test_refuses_non_release_zip(tmp_path, calls):
    prod, _ = install(tmp_path)
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as zf:
        zf.writestr("app/a.py", "x")
    assert au.apply(bad, prod) == 1


def test_failed_migration_returns_2_and_rollback_restores(tmp_path, calls, monkeypatch):
    prod, _ = install(tmp_path)
    repo2 = make_repo(tmp_path / "repo-0.0.2", "0.0.2")
    (repo2 / "app" / "a.py").write_text("V='0.0.2'", encoding="utf-8")
    z2 = release.build_zip(repo2, "0.0.2", "patch", tmp_path / "dist")

    def boom(root):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(au, "run_migrations", boom)
    assert au.apply(z2, prod) == 2
    assert (prod / "VERSION").read_text().strip() == "0.0.1"  # not advanced
    assert (prod / "app" / "a.py").read_text() == "V='0.0.2'"  # files already extracted
    assert au.rollback(prod) == 0
    assert (prod / "app" / "a.py").read_text() == "V='0.0.1'"
    assert (prod / ".env").read_text() == "SECRET=1"


def test_dry_run_changes_nothing(tmp_path, calls):
    prod, _ = install(tmp_path)
    repo2 = make_repo(tmp_path / "repo-0.0.2", "0.0.2")
    z2 = release.build_zip(repo2, "0.0.2", "patch", tmp_path / "dist")
    assert au.apply(z2, prod, dry_run=True) == 0
    assert (prod / "VERSION").read_text().strip() == "0.0.1" and calls == []


def test_backups_pruned_to_two(tmp_path, calls):
    prod, _ = install(tmp_path)
    for v in ["0.0.2", "0.0.3", "0.0.4"]:
        repo = make_repo(tmp_path / f"repo-{v}", v)
        z = release.build_zip(repo, v, "patch", tmp_path / "dist")
        assert au.apply(z, prod) == 0
    assert sorted(p.name for p in (prod / ".backup").iterdir()) == ["0.0.2", "0.0.3"]
```

- [ ] **Step 2: Run** → ImportError.

- [ ] **Step 3: Implement `apply_update.py`**

```python
"""Prod-side updater. Usage: python apply_update.py <release.zip> [--force] [--dry-run] | --rollback
Stdlib only. Run from the install root (or via deploy/update.ps1)."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROTECTED = {".env", "data", "logs", ".venv", ".backup"}
KEEP_BACKUPS = 2
MANIFEST = "manifest.json"


def _vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.strip().split("."))


def is_newer(new: str, current: str) -> bool:
    return _vt(new) > _vt(current)


def current_version(root: Path) -> str:
    vf = root / "VERSION"
    return vf.read_text(encoding="utf-8").strip() if vf.exists() else "0.0.0"


def read_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        if MANIFEST not in zf.namelist():
            raise ValueError("not a release archive: manifest.json missing")
        return json.loads(zf.read(MANIFEST))


def installed_manifest(root: Path) -> dict:
    p = root / MANIFEST
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"files": [], "lock_hash": ""}


def bot_running(root: Path) -> bool:
    pid_file = root / "data" / "bot.pid"
    if not pid_file.exists():
        return False
    try:
        pid = int(pid_file.read_text().strip())
    except ValueError:
        return False
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True,
                             text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _protected(rel: str) -> bool:
    top = rel.replace("\\", "/").split("/", 1)[0]
    return top in PROTECTED


def backup_app_layer(root: Path, version: str) -> Path:
    dest = root / ".backup" / version
    if dest.exists():
        shutil.rmtree(dest)
    for rel in installed_manifest(root)["files"] + ["VERSION", MANIFEST]:
        src = root / rel
        if src.is_file() and not _protected(rel):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    backups = sorted((root / ".backup").iterdir(), key=lambda p: _vt(p.name))
    for old in backups[:-KEEP_BACKUPS]:
        shutil.rmtree(old)
    return dest


def extract(zip_path: Path, root: Path) -> int:
    skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        for info in zf.infolist():
            name = info.filename
            if name.startswith("/") or ".." in Path(name).parts:
                raise ValueError(f"unsafe path in archive: {name}")
            if _protected(name) or name == "VERSION":
                skipped += 1
                continue
            zf.extract(info, root)
    return skipped


def prune_removed(root: Path, old_files: list[str], new_files: list[str]) -> list[str]:
    removed = []
    for rel in set(old_files) - set(new_files):
        p = root / rel
        if p.is_file() and not _protected(rel):
            p.unlink()
            removed.append(rel)
    return sorted(removed)


def uv_exe() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")


def venv_python(root: Path) -> Path:
    return root / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sync_runtime(root: Path) -> None:
    subprocess.run([uv_exe(), "sync", "--frozen", "--no-dev"], cwd=root, check=True)


def run_migrations(root: Path) -> None:
    subprocess.run([str(venv_python(root)), "-m", "tools.migrate", "--apply"], cwd=root, check=True)


def apply(zip_path: Path, root: Path, *, force: bool = False, dry_run: bool = False) -> int:
    try:
        new = read_manifest(zip_path)
    except (ValueError, zipfile.BadZipFile) as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    cur = current_version(root)
    old = installed_manifest(root)
    if not is_newer(new["version"], cur) and not force:
        print(f"refused: {new['version']} is not newer than installed {cur} (use --force)",
              file=sys.stderr)
        return 1
    if bot_running(root) and not force:
        print("refused: bot is running (data/bot.pid); stop it first or use --force", file=sys.stderr)
        return 1
    need_sync = new["lock_hash"] != old.get("lock_hash")
    print(f"update {cur} -> {new['version']} ({new['kind']}); runtime sync: {need_sync}")
    if dry_run:
        return 0
    backup = backup_app_layer(root, cur)
    print(f"backup  {backup}")
    try:
        skipped = extract(zip_path, root)
        removed = prune_removed(root, old["files"], new["files"])
        print(f"extracted; skipped {skipped} protected entries; removed {len(removed)} stale files")
        if need_sync:
            sync_runtime(root)
        run_migrations(root)
    except Exception as e:  # noqa: BLE001 — report and point at rollback
        print(f"FAILED: {e}\nrun: python apply_update.py --rollback", file=sys.stderr)
        return 2
    (root / "VERSION").write_text(new["version"] + "\n", encoding="utf-8")
    print(f"updated {cur} -> {new['version']}")
    return 0


def rollback(root: Path) -> int:
    backups = sorted((root / ".backup").glob("*"), key=lambda p: _vt(p.name)) if (root / ".backup").exists() else []
    if not backups:
        print("no backup to roll back to", file=sys.stderr)
        return 1
    src = backups[-1]
    for p in src.rglob("*"):
        if p.is_file():
            target = root / p.relative_to(src)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, target)
    print(f"rolled back app layer to {src.name}; database backups are in data/ (*.pre-*.sqlite)")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zip", nargs="?", type=Path)
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parent)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    a = ap.parse_args(argv)
    if a.rollback:
        return rollback(a.root)
    if not a.zip:
        ap.error("zip path required")
    return apply(a.zip, a.root, force=a.force, dry_run=a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
```

Note on `rollback`: the backup contains `VERSION`, so it restores the old version marker too. Note on `extract`: `VERSION` from the zip is skipped so it is only written after success.

- [ ] **Step 4: PowerShell wrappers**

`deploy/install.ps1`:
```powershell
param([Parameter(Mandatory)][string]$Zip, [Parameter(Mandatory)][string]$Dest)
$ErrorActionPreference = "Stop"
$uv = (Get-Command uv -ErrorAction SilentlyContinue).Source
if (-not $uv) { $uv = "$env:USERPROFILE\.local\bin\uv.exe" }
if (-not (Test-Path $uv)) { throw "uv not found; install: powershell -ExecutionPolicy ByPass -c `"irm https://astral.sh/uv/install.ps1 | iex`"" }
New-Item -ItemType Directory -Force $Dest | Out-Null
Expand-Archive -Path $Zip -DestinationPath $Dest -Force
Push-Location $Dest
try {
  & $uv sync --frozen --no-dev
  if (-not (Test-Path ".env")) { Copy-Item ".env.example" ".env"; Write-Host "created .env — fill in tokens before running" }
  New-Item -ItemType Directory -Force "data","logs" | Out-Null
  & ".venv\Scripts\python.exe" -m tools.migrate --apply --db "data\bot.sqlite"
  Write-Host "installed $(Get-Content VERSION) into $Dest. Start with deploy\run.ps1"
} finally { Pop-Location }
```

`deploy/update.ps1`:
```powershell
param([Parameter(Mandatory)][string]$Zip, [switch]$Force, [switch]$DryRun)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root ".venv\Scripts\python.exe"
$args = @((Resolve-Path $Zip).Path, "--root", $root)
if ($Force) { $args += "--force" }
if ($DryRun) { $args += "--dry-run" }
& $py (Join-Path $root "apply_update.py") @args
exit $LASTEXITCODE
```

`deploy/run.ps1`:
```powershell
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
& ".venv\Scripts\python.exe" -m app.main
```

- [ ] **Step 5: Run** tests + ruff (ruff must lint `apply_update.py` and `release.py` at the repo root — they are included by default). Manual: `uv run python apply_update.py --help`.
- [ ] **Step 6: Commit** `feat: prod updater with backup/rollback and deploy scripts`.

---

### Task 5: `RELEASE.md`, README, end-to-end dry run

**Files:**
- Create: `RELEASE.md`
- Modify: `README.md` (link), `documentation/ARCHITECTURE.md` (§ "Releases and migrations" short section pointing to RELEASE.md)

- [ ] **Step 1: Write `RELEASE.md`** covering: version rule (patch/full/auto), pre-release checklist (clean tree, tests), `uv run python release.py --auto`, push tags, first install (`deploy\install.ps1 -Zip dist\notion_ai_bot-0.0.2.zip -Dest C:\apps\notion_ai_bot`), update (`C:\apps\notion_ai_bot\deploy\update.ps1 <zip>`), what is protected, migrations authoring rules (new numbered file, never edit shipped ones, no BEGIN/COMMIT, additive DDL, `INSERT OR IGNORE` for data), `tools.migrate --status`, rollback (`apply_update.py --rollback`, DB from `data/*.pre-*.sqlite`), dev vs prod (repo vs install dir; same machine; prod has own `.env`, `.venv`, `data`), pid file contract (`data/bot.pid`, written by `app.main` in Plan 3).
- [ ] **Step 2: End-to-end check on this machine, no git side effects:** `uv run python release.py --patch --dry-run`; then build a zip without committing: `uv run python -c "import release, pathlib; print(release.build_zip(release.ROOT, '0.0.1', 'full', pathlib.Path('dist')))"`; `powershell -File deploy\install.ps1 -Zip dist\notion_ai_bot-0.0.1.zip -Dest $env:TEMP\nab_test`; confirm `$env:TEMP\nab_test\.venv` exists and `data\bot.sqlite` has `schema_migrations`; then `powershell -File $env:TEMP\nab_test\deploy\update.ps1 dist\notion_ai_bot-0.0.1.zip -DryRun` prints "not newer" (exit 1 — expected). Remove `$env:TEMP\nab_test` and `dist/` afterwards. Record the commands and output in the report.
- [ ] **Step 3: Commit** `docs: release, install, update and migration guide`.

---

## Self-review

- Spec coverage: versioning (T1), migrations + changesets + CLI (T2), release full/patch (T3), prod update + rollback + protected paths (T4), docs + real install smoke (T5). Startup verification hook (`assert_schema_current`) exists for Plan 3's `main.py`.
- Type consistency: `release.build_zip(root, version, kind, out_dir)` used identically in `tests/test_apply_update.py`; `apply_update.apply(zip, root, force=, dry_run=)`; manifest keys `version/kind/lock_hash/files` consumed by the updater; `tools.migrate --apply --db` used by `install.ps1` and `run_migrations`.
- Known simplifications: no down-migrations; app not restarted by the updater; `bot_running` relies on the pid file the bot will write in Plan 3.
