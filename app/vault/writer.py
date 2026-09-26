"""Writing to the vault: one action in, one file changed, and a way back.

Every write is atomic (temp file + rename) and remembers the file's previous text, so Undo puts
it back exactly. Nothing is ever deleted: a note the bot created is moved to the vault's own
`.trash`, which is where Obsidian's own delete puts it."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, ConfigDict

from app import texts
from app.vault import frontmatter, groceries, mdedit
from app.vault.frontmatter import render
from app.vault.index import SKIP_DIRS, VaultIndex
from app.vault.names import safe_name, unique

log = logging.getLogger(__name__)

TRASH_DIR = ".trash"
MAX_LINE = 2000


class VaultAction(BaseModel):
    """One thing to do in the vault. The filer produces these; they are checked against the
    index before they reach the writer (app/vault/filer.py)."""

    model_config = ConfigDict(extra="forbid")
    action: str  # task | note | append | update | rewrite | log | grocery | inbox
    text: str = ""  # the task's, log line's or inbox line's words
    note: str = ""  # which note to add to or change
    folder: str = ""
    title: str = ""
    heading: str = ""
    body: list[str] = []
    props: dict[str, str] = {}  # "" clears the property
    tags: list[str] = []
    due: str = ""
    repeat: str = ""
    countdown: bool = False
    done: bool | None = None
    task: str = ""
    # A question about dates: a day or a range to look at, and which kind of answer is wanted
    # ("day" = what is planned then, "now" = what to start with).
    due_from: str = ""
    due_to: str = ""
    scope: str = ""


class VaultUndo(BaseModel):
    """How to put one file back. `previous` is None when the file did not exist before."""

    model_config = ConfigDict(extra="forbid")
    path: str
    previous: str | None = None


class VaultWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: str
    path: str
    note: str
    # What exactly was written, when the note's name is not the interesting part: the
    # products a grocery write touched.
    detail: str = ""
    undo: VaultUndo | None = None

    @property
    def what(self) -> str:
        return texts.VAULT_WHAT.get(self.kind, "{note}").format(note=self.note,
                                                                 detail=self.detail)


def task_line(action: VaultAction, countdown_tag: str) -> str:
    parts = [action.text.strip()]
    parts += [f"#{t.lstrip('#')}" for t in action.tags]
    if action.countdown:
        parts.append(f"#{countdown_tag}")
    if action.repeat:
        parts.append(f"🔁 {action.repeat}")
    if action.due:
        parts.append(f"📅 {action.due}")
    mark = mdedit.DONE if action.done else mdedit.OPEN
    return f"- [{mark}] " + " ".join(p for p in parts if p)


class VaultWriter:
    def __init__(self, index: VaultIndex, *, countdown_tag: str = texts.VAULT_COUNTDOWN_TAG,
                 now: callable = datetime.now,
                 tag_source: Callable[[], str] | None = None) -> None:
        self._index = index
        self._root = index.root
        self._tag_source = tag_source or (lambda: countdown_tag)
        self._now = now

    # ---- files -----------------------------------------------------------------------

    def _path(self, rel: str) -> Path:
        path = (self._root / rel).resolve()
        root = self._root.resolve()
        # Nothing the index refuses to read may be written either: a note written into
        # Syncthing's archive would be invisible here and resurrected on the next sync.
        # `.trash` is the one exception: it is where this writer puts things itself.
        forbidden = SKIP_DIRS - {TRASH_DIR}
        parts = PurePosixPath(rel).parts
        if not path.is_relative_to(root) or any(p in forbidden for p in parts[:-1]):
            raise ValueError(f"refusing to write outside the vault's notes: {rel}")
        return path

    def _read(self, rel: str) -> str | None:
        try:
            return self._path(rel).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None

    def _write(self, rel: str, text: str, previous: str | None) -> VaultUndo:
        path = self._path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
        self._index.note_changed(rel)
        return VaultUndo(path=rel, previous=previous)

    def read(self, rel: str) -> str:
        return self._path(rel).read_text(encoding="utf-8")

    def replace(self, rel: str, text: str) -> None:
        """Rewrite a note the bot itself has just written (the linker's second pass). The undo
        record of the write that created it still points at the text from before it, so Undo
        takes the links away with the write they belong to."""
        self._write(rel, text, None)

    def undo(self, undo: VaultUndo) -> None:
        path = self._path(undo.path)
        if undo.previous is None:
            if path.exists():
                trash = self._path(f"{TRASH_DIR}/{PurePosixPath(undo.path).name}")
                trash.parent.mkdir(parents=True, exist_ok=True)
                os.replace(path, self._unused(trash))
        else:
            self._write(undo.path, undo.previous, None)
        self._index.note_changed(undo.path)

    @staticmethod
    def _unused(path: Path) -> Path:
        candidate, n = path, 1
        while candidate.exists():
            n += 1
            candidate = path.with_name(f"{path.stem} {n}{path.suffix}")
        return candidate

    # ---- actions ---------------------------------------------------------------------

    def run(self, action: VaultAction) -> VaultWrite:
        handler = {
            "task": self._task, "note": self._note, "append": self._append,
            "update": self._update, "rewrite": self._rewrite, "log": self._log,
            "grocery": self._grocery, "inbox": self._inbox,
        }.get(action.action)
        if handler is None:
            return self._inbox(action)
        return handler(action)

    def _task(self, action: VaultAction) -> VaultWrite:
        rel = f"{texts.VAULT_TASKS_NOTE}.md"
        previous = self._read(rel)
        line = task_line(action, self._tag_source())
        text = previous or ""
        if action.heading:
            updated = mdedit.append_to(text, [line], action.heading, create_heading=True)
        else:
            updated = mdedit.append_to(text, [line])
        undo = self._write(rel, updated or f"{text}\n{line}\n", previous)
        return VaultWrite(kind="task", path=rel, note=texts.VAULT_TASKS_NOTE, undo=undo)

    def _note(self, action: VaultAction) -> VaultWrite:
        folder = action.folder.strip("/") or texts.VAULT_NOTES_DIR
        taken = {n.name.casefold() for n in self._index.notes}
        name = unique(safe_name(action.title or action.text), taken)
        rel = f"{folder}/{name}.md" if folder else f"{name}.md"
        props = {k: v for k, v in action.props.items() if v != ""}
        if action.tags:
            props["tags"] = [t.lstrip("#") for t in action.tags]
        undo = self._write(rel, render(props, "\n".join(action.body)), None)
        return VaultWrite(kind="note", path=rel, note=name, undo=undo)

    def _append(self, action: VaultAction) -> VaultWrite:
        note = self._index.by_name(action.note)
        if note is None:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        lines = action.body or [action.text]
        updated = mdedit.append_to(previous, lines, action.heading or None)
        if updated is None:  # the heading is gone: the note's end is still the right place
            updated = mdedit.append_to(previous, lines) or previous
        undo = self._write(note.path, updated, previous)
        return VaultWrite(kind="append", path=note.path, note=note.name, undo=undo)

    def _update(self, action: VaultAction) -> VaultWrite:
        note = self._index.by_name(action.note)
        if note is None:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        text = previous
        changed = False
        if action.props:
            text = mdedit.set_props(text, {k: (None if v == "" else v)
                                           for k, v in action.props.items()})
            changed = True
        if action.task or action.done is not None or action.due:
            line = mdedit.find_task(text, action.task or action.text or note.name)
            if line is not None:
                text = mdedit.set_task(text, line, checked=action.done,
                                        due=action.due or None)
                changed = True
        if action.heading and action.body:
            replaced = mdedit.replace_section(text, action.heading, action.body)
            if replaced is not None:
                text, changed = replaced, True
        if not changed:
            return self._inbox(action)
        undo = self._write(note.path, text, previous)
        return VaultWrite(kind="update", path=note.path, note=note.name, undo=undo)

    def _rewrite(self, action: VaultAction) -> VaultWrite:
        """Replace a note's text (or one section of it) with text already written by the
        rewriter — the pipeline makes that call, because it is the only part of the vault
        side that has to read a note before it can write it.

        The old version also goes to `.trash`, where it outlives the undo window: this is
        the one write that can lose something the user spent an evening on."""
        note = self._index.by_name(action.note)
        if note is None or not action.body:
            return self._inbox(action)
        previous = self._read(note.path) or ""
        if action.heading:
            text = mdedit.replace_section(previous, action.heading, action.body)
            if text is None:  # the heading is gone: better to write nothing than all of it
                return self._inbox(action)
        else:
            props, _ = frontmatter.split(previous)
            text = frontmatter.render(props, "\n".join(action.body))
        self._to_trash(note.path, previous)
        undo = self._write(note.path, text, previous)
        return VaultWrite(kind="rewrite", path=note.path, note=note.name, undo=undo)

    def _to_trash(self, rel: str, text: str) -> None:
        """A copy of what a note said before, kept whatever happens to the undo record."""
        if not text.strip():
            return
        stamp = self._now().strftime("%Y-%m-%d %H%M%S")
        name = PurePosixPath(rel).stem
        path = self._unused(self._path(f"{TRASH_DIR}/{name} ({stamp}).md"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")

    def _grocery(self, action: VaultAction) -> VaultWrite:
        """Untick the products that have to be bought, or tick back the ones that were.

        Nothing is created and nothing is archived: the page holds one permanent line per
        product, and a message only changes which of them are ticked. A product the page
        has never heard of gets a line, which is how the registry learns."""
        rel = f"{texts.VAULT_GROCERIES_NOTE}.md"
        previous = self._read(rel)
        names = [n for n in (action.body or [action.text]) if n.strip()]
        done = bool(action.done)
        text, changed = groceries.apply(previous or groceries.note(), names, done=done)
        if not changed:  # already in the state that was asked for
            return VaultWrite(kind="grocery_none", path=rel,
                              note=texts.VAULT_GROCERIES_NOTE,
                              detail=", ".join(names))
        undo = self._write(rel, text, previous)
        return VaultWrite(kind="grocery_done" if done else "grocery", path=rel,
                          note=texts.VAULT_GROCERIES_NOTE,
                          detail=", ".join(changed), undo=undo)

    def _log(self, action: VaultAction) -> VaultWrite:
        day = self._now().strftime("%Y-%m-%d")
        rel = self._index.daily_note(day)
        previous = self._read(rel)
        line = f"- {self._now().strftime('%H:%M')} {action.text.strip()}"
        updated = mdedit.append_to(previous or "", [line]) or line
        undo = self._write(rel, updated, previous)
        return VaultWrite(kind="log", path=rel, note=day, undo=undo)

    def _inbox(self, action: VaultAction) -> VaultWrite:
        rel = f"{texts.VAULT_INBOX_NOTE}.md"
        previous = self._read(rel)
        line = f"- {(action.text or action.title or ' '.join(action.body)).strip()[:MAX_LINE]}"
        updated = mdedit.append_to(previous or "", [line]) or line
        undo = self._write(rel, updated, previous)
        return VaultWrite(kind="inbox", path=rel, note=texts.VAULT_INBOX_NOTE, undo=undo)
