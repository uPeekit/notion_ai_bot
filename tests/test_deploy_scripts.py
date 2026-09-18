"""The Windows launchers: they are plain text nobody runs in CI, so these checks pin the few
properties that break silently."""

import re
from pathlib import Path

import pytest

import release
from app import main

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = sorted([*ROOT.glob("deploy/*.ps1"), *ROOT.glob("*.cmd")])


@pytest.mark.parametrize("path", SCRIPTS, ids=lambda p: p.name)
def test_launchers_are_ascii(path):
    """Windows PowerShell 5.1 reads a BOM-less .ps1 as the ANSI code page, so a single em dash or
    Cyrillic letter corrupts the tokenizer — this already broke install.ps1 once."""
    data = path.read_bytes()
    bad = [i for i, b in enumerate(data) if b > 0x7F]
    assert not bad, f"{path.name}: non-ASCII byte at offset {bad[0]}"


def test_start_cmd_ships_in_the_release_and_calls_the_start_script():
    assert "start.cmd" in release.INCLUDE_GLOBS
    assert r"deploy\start.ps1" in (ROOT / "start.cmd").read_text(encoding="ascii")
    assert (ROOT / "deploy" / "start.ps1").exists()


def test_start_script_explains_every_exit_code_main_can_return():
    """start.ps1 turns app/main.py's exit codes into a plain-English line. A new EXIT_* constant
    without a matching case would fall through to 'stopped unexpectedly'."""
    exit_codes = {v for k, v in vars(main).items() if k.startswith("EXIT_")}
    script = (ROOT / "deploy" / "start.ps1").read_text(encoding="ascii")
    body = script[script.index("switch ($code)"):]
    handled = {int(n) for n in re.findall(r"^\s*(\d+)\s*\{", body, re.MULTILINE)}
    assert exit_codes <= handled, f"unhandled exit codes: {sorted(exit_codes - handled)}"


def test_start_script_does_not_stop_on_native_stderr():
    """Under $ErrorActionPreference = "Stop", Windows PowerShell 5.1 turns the first line a native
    command writes to stderr into a terminating error — and the bot logs to stderr — so the
    launcher would die on the bot's first log line."""
    script = (ROOT / "deploy" / "start.ps1").read_text(encoding="ascii")
    assert '$ErrorActionPreference = "Continue"' in script
    assert '$ErrorActionPreference = "Stop"' not in script


def test_release_ships_every_tracked_runtime_file():
    """0.1.1 shipped without app/admin/page.html — the globs only matched *.py — and the bot died
    at import. Any file git tracks under the runtime trees must reach the zip, whatever its
    extension, so the next non-Python asset can't repeat that."""
    import subprocess

    tracked = subprocess.run(
        ["git", "ls-files", "app", "tools", "migrations", "deploy"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.split()
    shipped = {p.as_posix() for p in release.collect_files(ROOT)}
    missing = sorted(set(tracked) - shipped)
    assert not missing, f"tracked but never released: {missing}"
