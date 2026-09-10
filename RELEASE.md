# Release, install, update

## Quick way (double-click)

- `release.cmd` in the repo root: shows current version and what `--auto` would do, asks kind / dry run / skip tests with defaults, runs `release.py`, then offers to push and to update (or freshly install) the production directory. Answers are remembered in `%USERPROFILE%\.notion_ai_bot\wizard.json`.
- `update.cmd` in the production install root: shows installed version and schema state, then a menu: update (default zip = newest `notion_ai_bot-*.zip` in the repo `dist/` or Downloads; a dry run is shown before confirming), dry run only, roll back, quit.

Every wizard prints the exact command it runs, so the manual commands below stay discoverable.


## Versioning rule

`pyproject.toml`'s `[project].version` (`MAJOR.MINOR.PATCH`) is the single source of truth; `app.version.get_version()` reads it (or a shipped `VERSION` file, if present, which takes priority). `release.py` bumps it:

- `--patch` — `PATCH += 1`. App-only change (`app/`, `tools/`, `migrations/`, `deploy/`, `apply_update.py`, `pyproject.toml`).
- `--full` — `MINOR += 1`, `PATCH = 0`. Dependency change (`uv.lock` touched) — the prod venv must be re-synced.
- `--auto` — inspects `git diff --name-only <last vX.Y.Z tag>..HEAD`: `uv.lock` changed → full; only app paths changed → patch; nothing relevant changed → prints `no app changes since <tag>` and exits 0 without building.
- `--set-version X.Y.Z` — explicit version, treated as a `full` release (always resyncs).

## Release checklist + commands

1. Clean working tree, tests green:
   ```
   uv run pytest -q
   uv run ruff check .
   ```
2. Build and tag (from repo root; refuses on a dirty tree unless `--allow-dirty`; runs pytest + ruff itself unless `--skip-tests`):
   ```
   uv run python release.py --auto
   ```
   This bumps `pyproject.toml`, runs `uv lock --offline`, commits `release: vX.Y.Z`, tags `vX.Y.Z` (skip with `--no-tag`), and writes `dist/notion_ai_bot-X.Y.Z.zip` + `dist/last_release.json`. Add `--dry-run` to see the computed version/kind without doing anything. Use `--patch`/`--full`/`--set-version` to override the auto classification.
3. Push:
   ```
   git push && git push --tags
   ```
4. Ship the zip from `dist/` to the target machine (or same machine, different directory).

## First install

```
deploy\install.ps1 -Zip dist\notion_ai_bot-0.0.2.zip -Dest C:\apps\notion_ai_bot
```

**Warning:** `install.ps1` is for a fresh, empty `-Dest` only. Never re-run it against an
existing install: unlike `apply_update.py`, it does no version check, takes no backup, does
not prune files the new zip removed, and offers no rollback if something goes wrong
mid-extract. To upgrade an existing install, use `deploy\update.ps1` (see Update, below).

Does, in order: locate `uv` (`PATH`, else `%USERPROFILE%\.local\bin\uv.exe`), extract the zip into `-Dest`, `uv sync --frozen --no-dev`, create `.env` from `.env.example` if missing (fill in tokens before running), create `data\` and `logs\`, then `.venv\Scripts\python.exe -m tools.migrate --apply --db data\bot.sqlite`. Start the bot with `deploy\run.ps1`.

## Update

From the **install directory** (not the repo):

```
C:\apps\notion_ai_bot\deploy\update.ps1 dist\notion_ai_bot-0.0.3.zip
```

Wraps `apply_update.py <zip> --root <install dir>`. Refuses (exit 1) if the zip's version is not newer than the installed `VERSION`, or if the bot looks running (`data\bot.pid` resolves to a live pid) — pass `-Force` to override either check. On success: backs up the current app layer to `.backup\<old-version>\` (keeps the 2 most recent), extracts the new files (protected paths skipped), deletes files present in the old manifest but absent from the new one, runs `uv sync --frozen --no-dev` only if `uv.lock`'s hash changed, then `python -m tools.migrate --apply`, then writes the new `VERSION`. Exit codes: `0` success, `1` refused (not newer / bot running / bad archive), `2` failure mid-update (partially applied — see Rollback). `-DryRun` reports what would happen and exits before any of that (still exits 1 if the "not newer"/"bot running" refusal applies, since that check runs before the dry-run short-circuit).

## What updates never touch

`apply_update.py`'s `PROTECTED` set — never extracted into, never deleted from, never touched by rollback:

```
.env  data  logs  .venv  .backup
```

## Rollback

- **App layer:** `python apply_update.py --rollback --root <install dir>` (or `deploy\update.ps1` has no rollback flag — call `apply_update.py` directly, or `.venv\Scripts\python.exe apply_update.py --rollback`). Copies files from the newest `.backup\<version>\` back over the app layer. Does not touch the database. Two things it does **not** do: it does not delete files the new version added (only the backed-up files are copied back over; anything new the failed update introduced stays on disk), and it does not re-sync the venv even if the failed update changed `uv.lock` — if the update that's being rolled back touched dependencies, re-run `uv sync --frozen --no-dev` by hand after rolling back.
- **Database:** migrations back up the DB before applying any pending file, as `data\<name>.pre-NNNN-<timestamp>.sqlite`. To roll back, stop the bot, copy the relevant `.pre-` file over `data\bot.sqlite` manually. There are no down-migrations — restoring the pre-migration snapshot is the only supported path backward. These `.pre-*.sqlite` snapshots are never pruned automatically and accumulate in `data\` across updates; delete the ones you no longer need by hand.

## Migrations authoring rules

- New file only: `migrations\NNNN_name.sql`, zero-padded 4-digit version, next integer after the highest existing one. Never edit or delete a migration that has shipped — `app/audit/migrate.py` stores a SHA-256 checksum per applied version in `schema_migrations` and raises on mismatch (the file changed after it was applied).
- Additive DDL only in practice: `CREATE TABLE IF NOT EXISTS`, `ALTER TABLE ... ADD COLUMN`, `CREATE INDEX IF NOT EXISTS`. Avoid destructive schema changes; there is no down-migration.
- Data-seeding statements must be idempotent: `INSERT OR IGNORE`, not bare `INSERT`.
- **Do not** write `BEGIN`, `COMMIT`, `ROLLBACK`, `END`, or `VACUUM` in a migration file — the runner wraps each file's SQL in its own transaction and rejects any file containing those tokens (`MigrationError`). This also rules out `CREATE TRIGGER ... BEGIN ... END;` — the linter's `END` check matches the trigger body's terminator too, so triggers are unavailable in migrations.
- The runner applies pending migrations in ascending version order inside one `sqlite3` connection per run and records each applied version's `(version, name, checksum, applied_at)` in `schema_migrations`.

## Checking schema state

```
.venv\Scripts\python.exe -m tools.migrate --status --db data\bot.sqlite
```

Prints `schema current` and exits 0 if nothing is pending; otherwise prints each `pending NNNN_name` and exits 2. `--dry-run` does the same computation as `--status` (equivalent output) and neither one applies any migration — but both still open the database file for writing: they create `data\bot.sqlite` if it doesn't exist yet (and its parent directory) and create the `schema_migrations` journal table if it's missing. Neither writes any migration row. `--apply` actually runs pending migrations (exit 0 on success, 1 on `MigrationError`, e.g. checksum mismatch or a bad migration file). `--db` defaults to `DB_PATH` from `.env`; `--dir` defaults to `migrations/` under the app root.

At startup, `AuditStore.assert_schema_current()` (wired into `app.main` in a later plan) raises if any migration is pending, so the bot refuses to run against a stale schema instead of silently misbehaving.

## Dev vs prod layout

Development is this repo (`C:\coding\notion_ai_bot`), driven with `uv run ...`. Production is a **separate directory** on the same or another Windows machine — e.g. `C:\apps\notion_ai_bot` — populated only by `deploy\install.ps1`/`deploy\update.ps1` from a release zip, never by `git clone`. The prod directory has its own `.env` (own tokens), its own `.venv` (own `uv sync --frozen --no-dev`, no dev dependencies), and its own `data\` (own SQLite DB, own backups) — none of that is shared with or derived from the dev checkout. Running dev and a prod install side by side on one machine is safe as long as they're different directories with different `.env`/`data`.

## Pid file contract

`data\bot.pid` is written by `app.main` on startup, holding the running process's pid — introduced in a later plan (Plan 3), not by this release tooling itself. Until `app.main` writes it, `apply_update.py`'s `bot_running()` check always finds no file and returns `False`, so update/rollback proceed unguarded by that check on this codebase's current state; once `app.main` writes the pid file, an update against a live process is refused unless `-Force` is passed.
