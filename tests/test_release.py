import json
import zipfile

import pytest

import release


def test_bump():
    assert release.bump("0.0.1", "patch") == "0.0.2"
    assert release.bump("0.0.9", "full") == "0.1.0"
    assert release.bump("1.4.7", "full") == "1.5.0"
    with pytest.raises(ValueError):
        release.bump("1.2", "patch")


def test_parse_version():
    assert release.parse_version("1.2.3") == (1, 2, 3)
    with pytest.raises(ValueError):
        release.parse_version("1.2")


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
        ".python-version": "3.12\n",
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
    assert ".python-version" in files
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
