"""Note and file names for the vault.

A note's file name is its title in Obsidian, so it keeps the user's words (Cyrillic included)
and loses only what a file system or Obsidian's link syntax cannot hold."""

from __future__ import annotations

import re

# Windows forbids \ / : * ? " < > | in a file name; Obsidian's link syntax breaks on # ^ [ ] |.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]+')
_SPACES = re.compile(r"\s+")
# Names Windows reserves whatever the extension.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}
MAX_NAME = 120
UNTITLED = "Untitled"


def safe_name(title: str, fallback: str = UNTITLED) -> str:
    """The title as a file name (without extension): forbidden characters become spaces, runs of
    whitespace collapse, and trailing dots and spaces go (Windows drops them silently, and two
    titles would then clash on disk without clashing here)."""
    name = _SPACES.sub(" ", _FORBIDDEN.sub(" ", title)).strip().rstrip(". ")
    name = name[:MAX_NAME].rstrip(". ")
    if not name or name.upper() in _RESERVED:
        return fallback
    return name


def unique(name: str, taken: set[str]) -> str:
    """`name`, or `name 2`, `name 3`, … — the first that is not in `taken` (compared without
    case, as Windows and macOS file systems do). The result is added to `taken`."""
    candidate, n = name, 1
    while candidate.casefold() in taken:
        n += 1
        candidate = f"{name} {n}"
    taken.add(candidate.casefold())
    return candidate
