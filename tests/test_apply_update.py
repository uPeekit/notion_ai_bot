import os
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


def test_prune_never_escapes_root(tmp_path):
    prod = tmp_path / "prod"
    prod.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    bad_abs = "C:/outside.txt" if os.name == "nt" else "/etc/passwd"
    removed = au.prune_removed(prod, ["../outside.txt", bad_abs], [])
    assert removed == []
    assert outside.exists()


@pytest.mark.skipif(os.name != "nt", reason="drive-letter paths only meaningful on Windows")
def test_extract_refuses_drive_letter_entry(tmp_path):
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("C:/evil.txt", "x")
    with pytest.raises(ValueError):
        au.extract(z, tmp_path / "root")


def test_backup_on_bare_root_does_not_crash(tmp_path):
    root = tmp_path / "bare"
    root.mkdir()
    dest = au.backup_app_layer(root, "0.0.0")
    assert dest == root / ".backup" / "0.0.0"
    assert dest.exists()
