"""Finding things in the vault, with no model call.

The filer decides that a message is a question ("what do I have for the house?"); the answer is
found here, by reading the notes. Names and aliases count most, then properties and tags, then
the text; open tick boxes are answered as tasks rather than as notes, because "what do I still
have to do" is the question they answer."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from app import texts
from app.vault.frontmatter import split
from app.vault.index import VaultIndex, overlap, words
from app.vault.mdedit import OPEN, task_lines

log = logging.getLogger(__name__)
_DATED = re.compile(r"^\d{4}-\d{2}-\d{2}")

MAX_HITS = 15
MAX_LINES_PER_NOTE = 2
MAX_FILE_BYTES = 400_000
NAME_SCORE = 3.0
PROP_SCORE = 2.0
TEXT_SCORE = 1.0


@dataclass(frozen=True)
class Hit:
    name: str
    path: str
    line: str = ""
    kind: str = "note"  # note | task

    def uri(self, vault: str) -> str:
        """An obsidian:// link: tapping it opens the note in the app, on the phone too."""
        return f"obsidian://open?vault={quote(vault)}&file={quote(self.path)}"


def _matching_lines(text: str, wanted: set[str], need: int = 1) -> list[str]:
    """Lines of the note's own text. Properties are matched separately and exactly, so the
    frontmatter is left out: a stem match there answered "which books am I reading" with books
    whose status was Read: four letters cannot tell Read from Reading."""
    out = []
    _, body = split(text)
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("---", "#")):
            continue
        if len(overlap(wanted, words(stripped))) >= need:
            out.append(stripped)
            if len(out) >= MAX_LINES_PER_NOTE:
                break
    return out


def _props_match(note, props: dict[str, str]) -> bool:
    """A property filter is exact (case aside): `status=Reading` means Reading, not Read."""
    for name, value in props.items():
        have = note.props.get(name)
        if isinstance(have, list):
            if not any(str(x).strip().casefold() == value.strip().casefold() for x in have):
                return False
        elif str(have or "").strip().casefold() != value.strip().casefold():
            return False
    return True


def search(index: VaultIndex, query: str, *, folder: str = "", tags: tuple[str, ...] = (),
           props: dict[str, str] | None = None, limit: int = MAX_HITS) -> list[Hit]:
    """Notes and open tasks that answer `query`, best first."""
    wanted = {w for w in words(query) if w not in texts.SEARCH_STOP_WORDS}
    # One matching word out of several is usually a coincidence (see index.related); with
    # two or more content words in the question, a hit has to carry at least two of them.
    need = 2 if len(wanted) > 2 else 1
    tags = tuple(t.lstrip("#").casefold() for t in tags)
    props = {k: v for k, v in (props or {}).items() if v.strip()}
    narrowed = bool(tags or props or folder)  # asked about a place, not only about words
    if not wanted and not narrowed:
        return []
    scored: list[tuple[float, Hit]] = []
    passed: list[tuple[str, str]] = []  # (name, path) of everything the filters let through
    in_place: list[tuple[str, str]] = []  # ... and what the folder alone let through
    tasks_note = index.by_name(texts.VAULT_TASKS_NOTE)
    for note in index.notes:
        if note.name.startswith("_") or (folder and not note.path.startswith(f"{folder}/")):
            continue
        if note is tasks_note or note.name == texts.VAULT_ARCHIVE_NOTE:
            continue  # the tasks note is answered as tasks below; the archive is finished work
        if props and not _props_match(note, props):
            continue
        in_place.append((note.name, note.path))
        if tags and not any(t.casefold() in tags for t in note.tags):
            continue
        passed.append((note.name, note.path))
        score = 0.0
        for name in note.names:
            # How much of the *question* this name answers — not how much of the name the
            # question covers, or a long title would always lose to a passing mention.
            hit = overlap(wanted, words(name))
            if hit:
                score = max(score, NAME_SCORE * len(hit) / max(len(wanted), 1))
        for value in note.props.values():
            if isinstance(value, str) and overlap(wanted, words(value)):
                score += PROP_SCORE
        lines: list[str] = []
        if note.size <= MAX_FILE_BYTES:
            try:
                lines = _matching_lines(index.read(note.path), wanted, need)
            except OSError:
                lines = []
        score += TEXT_SCORE * len(lines)
        if score:
            scored.append((score, Hit(note.name, note.path, lines[0] if lines else "")))
    if narrowed and not scored:
        # The filters are the answer: either nothing was asked but the place ("which books am
        # I reading"), or the words match nothing inside it ("when is the next meeting" in the
        # meetings folder). Listing what the filters let through beats answering nothing — and
        # a folder named together with a tag is two ways of saying the same place, not a
        # requirement that each note carry the tag as well.
        scored = _listed(passed or in_place)
    if tags or not (folder or props):
        # A task has no folder and no properties, so a question narrowed by those alone is not
        # about tasks — but a tag is exactly how tasks are filed, so a tag brings them back.
        scored += _task_hits(index, wanted, tags, tasks_note, need)
    scored.sort(key=lambda s: (-s[0], s[1].name))
    return [hit for _, hit in scored[:limit]]


def _listed(notes: list[tuple[str, str]]) -> list[tuple[float, Hit]]:
    """A plain listing of notes, newest first where the name starts with a date — that is how
    the meetings are named, and "when is the next one" wants the latest, not 2026-06-22."""
    dated = [n for n in notes if _DATED.match(n[0])]
    plain = [n for n in notes if not _DATED.match(n[0])]
    ordered = sorted(dated, reverse=True) + sorted(plain)
    # Descending scores keep this order through the caller's own sort.
    return [(1.0 + (len(ordered) - i) / (len(ordered) + 1), Hit(name, path))
            for i, (name, path) in enumerate(ordered)]


def _task_hits(index: VaultIndex, wanted: set[str], tags: tuple[str, ...], tasks_note,
               need: int = 1) -> list[tuple[float, Hit]]:
    if tasks_note is None or not (wanted or tags):
        return []
    try:
        lines = task_lines(index.read(tasks_note.path))
    except OSError:
        return []
    out = []
    for _, mark, line in lines:
        if mark != OPEN:
            continue
        line_tags = {w.lstrip("#").casefold() for w in line.split() if w.startswith("#")}
        if tags and not (line_tags & set(tags)):
            continue
        if wanted and len(overlap(wanted, words(line))) < need:
            continue  # a tag alone (tags and no words) already narrowed it above
        out.append((NAME_SCORE, Hit(tasks_note.name, tasks_note.path, line.strip(), "task")))
    return out


def vault_name(root: Path) -> str:
    return root.name
