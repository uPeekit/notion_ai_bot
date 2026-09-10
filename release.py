"""Build a versioned release zip. Usage: uv run python release.py --patch|--full|--auto"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
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
    "documentation/NOTION_SETUP.md", ".python-version", "update.cmd",
]
APP_PREFIXES = ("app/", "tools/", "migrations/", "deploy/", "apply_update.py", "pyproject.toml")
Kind = Literal["patch", "full"]
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")


def uv_exe() -> str:
    return shutil.which("uv") or str(Path.home() / ".local" / "bin" / "uv.exe")


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def parse_version(version: str) -> tuple[int, int, int]:
    if not _VERSION_RE.fullmatch(version):
        raise ValueError(f"bad version {version!r}")
    major, minor, patch = (int(x) for x in version.split("."))
    return major, minor, patch


def bump(version: str, kind: Kind) -> str:
    major, minor, patch = parse_version(version)
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

    from app.version import read_pyproject_version

    current = read_pyproject_version(ROOT / "pyproject.toml")
    tag = last_tag()
    if a.set_version:
        try:
            parse_version(a.set_version)
        except ValueError:
            print(f"invalid --set-version {a.set_version!r}; expected MAJOR.MINOR.PATCH",
                  file=sys.stderr)
            return 1
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
    dirty = bool(git("status", "--porcelain"))
    if a.dry_run:
        if dirty:
            print("note: working tree is dirty")
        return 0
    if dirty and not a.allow_dirty:
        print("working tree is dirty; commit first or pass --allow-dirty", file=sys.stderr)
        return 1
    if not a.skip_tests:
        subprocess.run([uv_exe(), "run", "pytest", "-q"], cwd=ROOT, check=True)
        subprocess.run([uv_exe(), "run", "ruff", "check", "."], cwd=ROOT, check=True)

    set_pyproject_version(ROOT / "pyproject.toml", version)
    lock_result = subprocess.run([uv_exe(), "lock", "--offline"], cwd=ROOT, check=False,
                                  capture_output=True, text=True)
    if lock_result.returncode != 0:
        print(f"warning: uv lock failed: {lock_result.stderr}", file=sys.stderr)
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
