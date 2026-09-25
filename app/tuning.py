"""Settings the user edits on the admin page rather than in `.env` or in code.

Everything here is vocabulary or wording — the things you want to adjust after living with the
bot for a week, without a release and without a restart. Stored in one JSON file next to the
database, re-read whenever it changes, and falling back to what `.env` and the code say.

What is deliberately *not* here: the system prompts themselves. They are long, interdependent
and covered by tests, and a stray edit would degrade every message quietly. The pattern that
works is a small structured text the prompt reads — the workspace note, `_bot.md`, the mail
buckets, and the two "extra instructions" fields below."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

MAX_TEXT = 2000
MAX_WORDS = 100


@dataclass(frozen=True)
class Defaults:
    """What each field falls back to: `.env` for the first three, the code for the rest."""

    bot_name: str
    agenda_at: str
    mail_at: str
    web_words: tuple[str, ...]
    countdown_tag: str
    date_props: tuple[str, ...]


class Tuning:
    def __init__(self, path: Path, defaults: Defaults) -> None:
        self._path = path
        self._defaults = defaults
        self._values: dict[str, object] = {}
        self._mtime: float | None = None
        self.reload()

    # ---- file ------------------------------------------------------------------------

    def reload(self) -> None:
        try:
            stat = self._path.stat()
        except OSError:
            self._values, self._mtime = {}, None
            return
        if self._mtime == stat.st_mtime:
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._values = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError) as e:
            log.warning("could not read %s (%s); using .env and the defaults", self._path, e)
            self._values = {}
        self._mtime = stat.st_mtime

    def save(self, values: dict) -> int:
        """Write what the page posted. Returns how many fields actually changed; an empty
        value means "use the default", so clearing a field is how you reset it."""
        current = self.all()
        wanted = dict(current)
        for key in current:
            if key in values:
                wanted[key] = _clean(key, values[key])
        changed = sum(1 for k in current if wanted[k] != current[k])
        if not changed:
            return 0
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps(wanted, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self._path)
            self._values = wanted
            self._mtime = self._path.stat().st_mtime
        except OSError as e:
            log.warning("could not save %s: %s", self._path, e)
            return 0
        log.info("tuning updated: %d field(s)", changed)
        return changed

    def all(self) -> dict:
        """Every field as the page shows it — the saved value, or the default behind it."""
        return {
            "bot_name": self.bot_name,
            "agenda_at": self.agenda_at,
            "mail_at": self.mail_at,
            "web_words": ", ".join(self.web_words),
            "countdown_tag": self.countdown_tag,
            "date_props": ", ".join(self.date_props),
            "research_note": self.research_note,
            "linker_note": self.linker_note,
        }

    # ---- fields ----------------------------------------------------------------------

    def _text(self, key: str, default: str) -> str:
        self.reload()
        value = self._values.get(key)
        return value.strip() if isinstance(value, str) and value.strip() else default

    def _list(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        self.reload()
        value = self._values.get(key)
        if not isinstance(value, str) or not value.strip():
            return default
        parts = [p.strip().lstrip("#") for p in value.replace("\n", ",").split(",")]
        return tuple(p for p in parts if p)[:MAX_WORDS] or default

    @property
    def bot_name(self) -> str:
        return self._text("bot_name", self._defaults.bot_name)

    @property
    def agenda_at(self) -> str:
        return self._text("agenda_at", self._defaults.agenda_at)

    @property
    def mail_at(self) -> str:
        return self._text("mail_at", self._defaults.mail_at)

    @property
    def web_words(self) -> tuple[str, ...]:
        return self._list("web_words", self._defaults.web_words)

    @property
    def countdown_tag(self) -> str:
        return self._text("countdown_tag", self._defaults.countdown_tag).lstrip("#")

    @property
    def date_props(self) -> tuple[str, ...]:
        return self._list("date_props", self._defaults.date_props)

    @property
    def research_note(self) -> str:
        """Extra instructions appended to the web-research prompt. Empty by default."""
        return self._text("research_note", "")

    @property
    def linker_note(self) -> str:
        """Extra instructions appended to the linker's prompt. Empty by default."""
        return self._text("linker_note", "")


def _clean(key: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{key!r} must be a string")
    return " ".join(value.split()) if key in ("bot_name", "agenda_at", "mail_at",
                                               "countdown_tag") else value.strip()[:MAX_TEXT]
