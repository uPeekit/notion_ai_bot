"""Prod-side updater. Usage: python apply_update.py <release.zip> [--force] [--dry-run] | --rollback
Stdlib only. Run from the install root (or via deploy/update.ps1)."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

PROTECTED = {".env", "data", "logs", ".venv", ".backup"}
KEEP_BACKUPS = 2
MANIFEST = "manifest.json"
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def _vt(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.strip().split("."))


def is_newer(new: str, current: str) -> bool:
    return _vt(new) > _vt(current)


def current_version(root: Path) -> str:
    vf = root / "VERSION"
    if not vf.exists():
        return "0.0.0"
    v = vf.read_text(encoding="utf-8").strip()
    return v if _VERSION_RE.fullmatch(v) else "0.0.0"


def read_manifest(zip_path: Path) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        if MANIFEST not in zf.namelist():
            raise ValueError("not a release archive: manifest.json missing")
        manifest = json.loads(zf.read(MANIFEST))
    version = manifest.get("version")
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise ValueError("not a release archive: manifest.json 'version' is invalid")
    if manifest.get("kind") not in ("patch", "full"):
        raise ValueError("not a release archive: manifest.json 'kind' must be 'patch' or 'full'")
    lock_hash = manifest.get("lock_hash")
    if not isinstance(lock_hash, str) or not lock_hash:
        raise ValueError("not a release archive: manifest.json 'lock_hash' must be non-empty")
    files = manifest.get("files")
    if not isinstance(files, list) or not all(isinstance(f, str) for f in files):
        raise ValueError("not a release archive: manifest.json 'files' must be a list of strings")
    return manifest


def installed_manifest(root: Path) -> dict:
    p = root / MANIFEST
    if not p.exists():
        return {"files": [], "lock_hash": ""}
    return json.loads(p.read_text(encoding="utf-8"))


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


def _safe_rel(rel: str) -> bool:
    """Reject paths that are absolute, carry a drive/root, or climb out of root via '..'."""
    p = Path(rel)
    if p.is_absolute() or p.drive or p.root or ".." in p.parts:
        return False
    return bool(rel) and not rel.startswith(("/", "\\"))


def _sorted_backups(backup_dir: Path) -> list[Path]:
    return sorted(
        (p for p in backup_dir.iterdir() if p.is_dir() and _VERSION_RE.fullmatch(p.name)),
        key=lambda p: _vt(p.name),
    )


def backup_app_layer(root: Path, version: str) -> Path:
    backup_dir = root / ".backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    dest = backup_dir / version
    if dest.exists():
        return dest
    dest.mkdir(parents=True, exist_ok=True)
    for rel in installed_manifest(root)["files"] + ["VERSION", MANIFEST]:
        if not _safe_rel(rel):
            continue
        src = root / rel
        if src.is_file() and not _protected(rel):
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
    backups = _sorted_backups(backup_dir)
    for old in backups[:-KEEP_BACKUPS]:
        shutil.rmtree(old)
    return dest


def extract(zip_path: Path, root: Path) -> int:
    skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        infos = zf.infolist()
        for info in infos:
            if not _safe_rel(info.filename):
                raise ValueError(f"unsafe path in archive: {info.filename}")
        for info in infos:
            name = info.filename
            if _protected(name) or name == "VERSION":
                skipped += 1
                continue
            zf.extract(info, root)
    return skipped


def prune_removed(root: Path, old_files: list[str], new_files: list[str]) -> list[str]:
    removed = []
    for rel in set(old_files) - set(new_files):
        if not _safe_rel(rel):
            continue
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
        print("refused: bot is running (data/bot.pid); stop it first or use --force",
              file=sys.stderr)
        return 1
    need_sync = new["lock_hash"] != old.get("lock_hash")
    print(f"update {cur} -> {new['version']} ({new['kind']}); runtime sync: {need_sync}")
    if dry_run:
        return 0
    installed_version = old.get("version")
    if installed_version and installed_version != cur:
        print(f"root is mid-update (manifest {installed_version}, VERSION {cur}); "
              "keeping existing backup")
    else:
        backup = backup_app_layer(root, cur)
        print(f"backup  {backup}")
    try:
        skipped = extract(zip_path, root)
        removed = prune_removed(root, old["files"], new["files"])
        print(f"extracted; skipped {skipped} protected entries; removed {len(removed)} stale files")
        if need_sync:
            sync_runtime(root)
        run_migrations(root)
    except Exception as e:
        print(f"FAILED: {e}\nrun: python apply_update.py --rollback", file=sys.stderr)
        return 2
    (root / "VERSION").write_text(new["version"] + "\n", encoding="utf-8")
    print(f"updated {cur} -> {new['version']}")
    return 0


def rollback(root: Path) -> int:
    backup_dir = root / ".backup"
    backups = _sorted_backups(backup_dir) if backup_dir.exists() else []
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
