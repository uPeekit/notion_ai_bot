"""On/off switches the admin page can flip, without editing `.env` or restarting the bot.

`.env` holds what they are when the file is not there yet; the file, once written, wins. They
are read per message, so a flip takes effect on the next one."""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

# notion:   the Notion pipeline (interpreter, questions, writes)
# obsidian: the vault pipeline
# linker:   the pass that adds [[links]] to a note after the reply
# mail:     the read-only inbox digest
NAMES = ("notion", "obsidian", "linker", "mail")


class Switches:
    def __init__(self, path: Path, defaults: dict[str, bool] | None = None) -> None:
        self._path = path
        self._defaults = {name: True for name in NAMES} | dict(defaults or {})
        self._values: dict[str, bool] = dict(self._defaults)
        self._mtime: float | None = None
        self.reload()

    def reload(self) -> None:
        """Re-read the file if it changed. Unreadable or nonsense: keep what .env said, and say
        so once — a broken file must not silently switch half the bot off."""
        try:
            stat = self._path.stat()
        except OSError:
            self._values, self._mtime = dict(self._defaults), None
            return
        if self._mtime == stat.st_mtime:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("not an object")
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); using the settings from .env", self._path, e)
            self._values = dict(self._defaults)
            return
        self._values = {name: bool(raw.get(name, self._defaults[name])) for name in NAMES}
        self._mtime = stat.st_mtime

    def get(self, name: str) -> bool:
        self.reload()
        return self._values.get(name, True)

    def all(self) -> dict[str, bool]:
        self.reload()
        return dict(self._values)

    def save(self, values: dict[str, bool]) -> int:
        """Write the switches the page posted; returns how many actually changed."""
        current = self.all()
        wanted = {name: bool(values.get(name, current[name])) for name in NAMES}
        changed = sum(1 for name in NAMES if wanted[name] != current[name])
        if changed:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(wanted, indent=1), encoding="utf-8")
            tmp.replace(self._path)
            self._values, self._mtime = wanted, self._path.stat().st_mtime
            log.info("switches: %s", ", ".join(f"{k}={'on' if v else 'off'}"
                                                for k, v in wanted.items()))
        return changed
