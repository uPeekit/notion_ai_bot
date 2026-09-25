"""The buckets a mail digest is sorted into, and what belongs in each — the user's text.

Edited on the admin page and re-read on every run, so a change takes effect at the next digest
with no restart. The file wins; `.env` (or the built-in default) is only what it starts from."""

from __future__ import annotations

import logging
from pathlib import Path

from app.mail.classify import parse_buckets

log = logging.getLogger(__name__)

MAX_TEXT = 4000


class Buckets:
    def __init__(self, path: Path, default: str) -> None:
        self._path = path
        self._default = default

    def load(self) -> str:
        try:
            text = self._path.read_text(encoding="utf-8").strip()
        except OSError:
            return self._default
        return text or self._default

    def save(self, text: str) -> bool:
        """True when something actually changed. An empty text resets to the default."""
        cleaned = text.strip()[:MAX_TEXT]
        if cleaned == self.load().strip():
            return False
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(cleaned, encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not save the mail buckets: %s", e)
            return False
        log.info("mail buckets updated (%d of them)", len(self.parsed()[0]))
        return True

    def parsed(self) -> tuple[list[str], dict[str, str]]:
        return parse_buckets(self.load())
