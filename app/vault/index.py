"""What is in the vault: every note's name, aliases, tags, properties and headings.

Built from the file system and refreshed by modification time, so a message costs a `stat` per
note and a read of only what changed. Three parts of the Obsidian side use it: the filer (which
note a message means, which folders and tags exist), the deterministic check of what the filer
answered, and the linker (which names a new note could link to)."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from app import texts
from app.vault import frontmatter

log = logging.getLogger(__name__)

SKIP_DIRS = {".obsidian", ".trash", ".git", ".stfolder", ".sync"}
GUIDE_NOTE = "_bot"
# A word of this length or more may carry a link on its own (see `words`); shorter ones only
# count inside a longer name.
MIN_WORD = 4
MAX_NOTES = 5000
_WORD = re.compile(r"[^\W\d_][\w-]*", re.UNICODE)
_TAG = re.compile(r"(?:^|\s)#([^\W\d_][\w/-]*)", re.UNICODE)
_HEADING = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
_FENCE = re.compile(r"^\s*(```|~~~)")


def words(text: str) -> set[str]:
    return {w.casefold() for w in _WORD.findall(text) if len(w) >= MIN_WORD}


def stems(text: str) -> set[str]:
    """Words cut to their first few letters, which is all the matching Russian endings allow
    without a morphology library: a Russian name and the same name inflected share a stem.
    Enough to pick the notes worth showing the model, and never the last word on anything."""
    return {w[:MIN_WORD] for w in words(text)}


@dataclass(frozen=True)
class Note:
    path: str  # vault-relative, posix
    name: str  # the file's stem: what [[links]] use
    folder: str
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    props: dict = field(default_factory=dict)
    headings: tuple[str, ...] = ()
    mtime: float = 0.0
    size: int = 0

    @property
    def names(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)


def parse(path: str, text: str, mtime: float = 0.0, size: int = 0) -> Note:
    props, body = frontmatter.split(text)
    raw_aliases = props.get("aliases") or props.get("alias") or []
    if isinstance(raw_aliases, str):
        raw_aliases = [raw_aliases]
    raw_tags = props.get("tags") or props.get("tag") or []
    if isinstance(raw_tags, str):
        raw_tags = [raw_tags]
    tags = {str(t).lstrip("#") for t in raw_tags if str(t).strip()}
    headings: list[str] = []
    fenced = False
    for line in body.splitlines():
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = _HEADING.match(line)
        if m:
            headings.append(m.group(2))
            continue
        tags.update(t for t in _TAG.findall(line))
    pure = PurePosixPath(path)
    return Note(path=path, name=pure.stem, folder=str(pure.parent) if pure.parent.name else "",
                aliases=tuple(str(a) for a in raw_aliases if str(a).strip()),
                tags=tuple(sorted(tags)), props=props, headings=tuple(headings),
                mtime=mtime, size=size)


class VaultIndex:
    """One index per vault, kept between messages. `refresh()` is cheap and idempotent."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._notes: dict[str, Note] = {}

    # ---- building --------------------------------------------------------------------

    def refresh(self) -> None:
        if not self.root.is_dir():
            self._notes = {}
            return
        seen: set[str] = set()
        for path in sorted(self.root.rglob("*.md")):
            rel = path.relative_to(self.root).as_posix()
            if any(part in SKIP_DIRS for part in PurePosixPath(rel).parts):
                continue
            if len(seen) >= MAX_NOTES:
                log.warning("vault has more than %d notes; the rest are not indexed", MAX_NOTES)
                break
            seen.add(rel)
            try:
                stat = path.stat()
                known = self._notes.get(rel)
                if known and known.mtime == stat.st_mtime and known.size == stat.st_size:
                    continue
                self._notes[rel] = parse(rel, path.read_text(encoding="utf-8"),
                                          stat.st_mtime, stat.st_size)
            except OSError as e:  # a file being written or moved right now: keep what we have
                log.info("could not read %s (%s)", rel, type(e).__name__)
        for gone in set(self._notes) - seen:
            del self._notes[gone]

    def note_changed(self, rel: str) -> None:
        """Re-read one note after the bot itself wrote it (cheaper than a full refresh)."""
        path = self.root / rel
        try:
            stat = path.stat()
            self._notes[rel] = parse(rel, path.read_text(encoding="utf-8"), stat.st_mtime,
                                      stat.st_size)
        except OSError:
            self._notes.pop(rel, None)

    # ---- reading ---------------------------------------------------------------------

    @property
    def notes(self) -> list[Note]:
        return list(self._notes.values())

    def get(self, rel: str) -> Note | None:
        return self._notes.get(rel)

    def by_name(self, name: str) -> Note | None:
        """The note a name or alias means. An exact match on the name wins over an alias, and
        a shorter path wins over a deeper one when two notes share a name."""
        wanted = name.strip().strip("[]").casefold()
        if not wanted:
            return None
        exact = [n for n in self._notes.values() if n.name.casefold() == wanted]
        if not exact:
            exact = [n for n in self._notes.values()
                     if any(a.casefold() == wanted for a in n.aliases)]
        return min(exact, key=lambda n: (n.path.count("/"), n.path)) if exact else None

    def folders(self) -> list[str]:
        """Every folder that holds notes, and every folder on the way to one."""
        out: set[str] = set()
        for note in self._notes.values():
            parts = PurePosixPath(note.path).parent.parts
            for i in range(1, len(parts) + 1):
                out.add("/".join(parts[:i]))
        return sorted(x for x in out if x)

    def tags(self) -> list[str]:
        return sorted({t for n in self._notes.values() for t in n.tags})

    def candidates(self, text: str, limit: int = 40) -> list[Note]:
        """Notes whose name or aliases share words with `text`, best first — what the filer is
        shown so it can pick a note to add to or update."""
        wanted = stems(text)
        if not wanted:
            return []
        scored: list[tuple[float, Note]] = []
        for note in self._notes.values():
            if note.name == GUIDE_NOTE:
                continue
            best = 0.0
            for name in note.names:
                nw = stems(name)
                if not nw:
                    continue
                hit = len(nw & wanted)
                if hit:
                    best = max(best, hit / len(nw))
            if best:
                scored.append((best, note))
        scored.sort(key=lambda s: (-s[0], s[1].name))
        return [n for _, n in scored[:limit]]

    def guide(self) -> str:
        """The user's own instructions to the bot (`_bot.md`), without its frontmatter."""
        note = self.by_name(GUIDE_NOTE)
        if note is None:
            return ""
        try:
            _, body = frontmatter.split((self.root / note.path).read_text(encoding="utf-8"))
        except OSError:
            return ""
        return body.strip()

    def read(self, rel: str) -> str:
        return (self.root / rel).read_text(encoding="utf-8")

    def daily_note(self, day: str) -> str:
        return f"{texts.VAULT_DAILY_DIR}/{day}.md"
