import re

from app.version import app_root, get_version, read_pyproject_version


def test_app_root_contains_app_package():
    assert (app_root() / "app" / "__init__.py").exists()


def test_pyproject_version_has_semver_shape():
    version = read_pyproject_version(app_root() / "pyproject.toml")
    assert re.fullmatch(r"\d+\.\d+\.\d+", version)
    if not (app_root() / "VERSION").exists():
        assert get_version() == version


def test_version_prefers_version_file(tmp_path, monkeypatch):
    (tmp_path / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    pyproject_content = '[project]\nname="x"\nversion="1.2.3"\n'
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
    monkeypatch.setattr("app.version.app_root", lambda: tmp_path)
    assert get_version() == "9.9.9"


def test_version_falls_back_to_pyproject_then_dev(tmp_path, monkeypatch):
    pyproject_content = '[project]\nname="x"\nversion="1.2.3"\n'
    (tmp_path / "pyproject.toml").write_text(pyproject_content, encoding="utf-8")
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
