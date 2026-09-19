"""The user-owned half of a target's metadata: descriptions, required-field flags and the inbox
choice, kept in `data/targets.yaml` next to the discovered half that Notion owns.

Two independent writers share that one file — the event loop, through `Discovery._refresh_locked`
-> `ensure()`, and the admin server's HTTP thread, through `load()` -> merge -> `save()` — so
every public method here takes `_lock` and `save()` writes through a temp file in the same
directory plus `os.replace`. Neither is belt-and-braces. Without the lock, `ensure()` can read a
file the admin thread has already truncated but not finished rewriting, get a *partial but still
valid* mapping, re-add the discovered names to it and save that back — silently dropping the
descriptions and the inbox flag the admin page exists to edit. Without the atomic write there is
a truncated file for a reader to see in the first place.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

log = logging.getLogger(__name__)


class FieldMeta(BaseModel):
    name: str = ""
    description: str = ""
    required: bool = False
    # option id -> what the option means, for select/multi_select/status fields. An option with
    # a description may be chosen by meaning; one without only when the message names it.
    options: dict[str, str] = Field(default_factory=dict)


class TargetMeta(BaseModel):
    name: str = ""
    description: str = ""
    fields: dict[str, FieldMeta] = Field(default_factory=dict)
    inbox: bool = False  # user-set fallback flag; Descriptions.ensure never touches it, so it
                         # survives every discovery round trip (editable from the admin page,
                         # Plan 3b)
    local_only: bool = False  # user-set: Claude never sees this target's description or
                              # contents, and a message it routes here is re-read locally
    hidden: bool = False  # user-set: left out of the model's context entirely (e.g. a page
                          # that is only a filtered view of a database)


class Descriptions:
    def __init__(self, path: Path) -> None:
        self._path = path
        # Re-entrant because ensure() holds it across its own load()/save() calls, which is the
        # whole point: the read, the merge and the write are one critical section, not three.
        self._lock = threading.RLock()
        self.broken = False

    def load(self) -> dict[str, TargetMeta]:
        with self._lock:
            return self._load_locked()

    def save(self, meta: dict[str, TargetMeta]) -> None:
        with self._lock:
            self._save_locked(meta)

    def ensure(self, discovered: dict[str, tuple[str, dict[str, str]]]) -> dict[str, TargetMeta]:
        with self._lock:
            return self._ensure_locked(discovered)

    # ---- internals (callers already hold _lock) -----------------------------------------------

    def _load_locked(self) -> dict[str, TargetMeta]:
        if not self._path.exists():
            self.broken = False
            return {}
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError:
            log.warning("targets file %s is malformed; ignoring descriptions", self._path)
            self.broken = True
            return {}
        if not isinstance(raw, dict):
            log.warning("targets file %s is malformed; ignoring descriptions", self._path)
            self.broken = True
            return {}
        self.broken = False
        return {str(k): TargetMeta.model_validate(v or {}) for k, v in raw.items()}

    def _save_locked(self, meta: dict[str, TargetMeta]) -> None:
        """Serialise first, then swap the finished file into place with `os.replace`, which is
        atomic on both POSIX and Windows. A reader therefore only ever sees the whole previous
        document or the whole new one — never the truncated middle `Path.write_text` leaves
        behind for as long as it takes to write the bytes. The temp file is created in the same
        directory so the replace stays within one filesystem, and is removed again if anything
        between its creation and the swap fails."""
        data = {k: v.model_dump() for k, v in meta.items()}
        _atomic_write(self._path, yaml.safe_dump(data, allow_unicode=True, sort_keys=False))

    def _ensure_locked(
        self, discovered: dict[str, tuple[str, dict[str, str]]]
    ) -> dict[str, TargetMeta]:
        meta = self._load_locked()
        broken = self.broken
        before = {k: v.model_dump() for k, v in meta.items()}
        for tid, (name, fields) in discovered.items():
            t = meta.setdefault(tid, TargetMeta())
            t.name = name
            for fid, fname in fields.items():
                f = t.fields.setdefault(fid, FieldMeta())
                f.name = fname
        if not broken and {k: v.model_dump() for k, v in meta.items()} != before:
            self._save_locked(meta)
        return meta


class WorkspaceNote:
    """The user's free-text note about how their workspace is organised ("all tasks live in
    TODO; the per-topic pages are views of it by tag"), sent with every message. A plain text
    file beside targets.yaml; read on each message, so an edit applies to the next one."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str:
        try:
            return self._path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""
        except OSError as e:
            log.warning("workspace note %s unreadable (%s); sending none", self._path, e)
            return ""

    def save(self, text: str) -> None:
        text = text.strip()
        _atomic_write(self._path, text + "\n" if text else "")


def _atomic_write(path: Path, text: str) -> None:
    """Write through a temp file in the same directory and swap it in with `os.replace`, atomic
    on POSIX and Windows alike: a reader sees the whole old file or the whole new one, never the
    truncated middle `Path.write_text` leaves behind while it writes. The temp file is removed
    again if anything before the swap fails."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise
