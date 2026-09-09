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
