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
