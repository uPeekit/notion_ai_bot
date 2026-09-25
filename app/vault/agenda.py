"""What is due, and what to do next — read from the vault, with no model call.

The morning digest, the answer to "what do I have on Friday" and the answer to "what should I
do now" all come from here. A model only ever decides *that* a message is one of those
questions; the facts are read from the files, so the bot cannot invent a task or a date."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from app import texts
from app.vault.index import VaultIndex
from app.vault.mdedit import OPEN, task_lines

log = logging.getLogger(__name__)

DATE_PROPS = texts.VAULT_DATE_PROPS
MAX_SUGGESTIONS = 5
MAX_PER_SECTION = 15
_DUE = re.compile(r"📅\s*(\d{4}-\d{2}-\d{2})")
_DONE_DATE = re.compile(r"✅\s*(\d{4}-\d{2}-\d{2})")
# A time the user typed at the start of the task ("9.20 dentist", "13 check-up"):
# the Tasks plugin stores dates only, so this is where a time of day comes from.
_TIME = re.compile(r"^(\d{1,2})[:.](\d{2})\b")
_TAG = re.compile(r"#([^\W\d_][\w/-]*)", re.UNICODE)
_PRIORITY = {"🔺": 0, "⏫": 1, "🔼": 2, "🔽": 4, "⏬": 5}
NORMAL_PRIORITY = 3


@dataclass(frozen=True)
class Item:
    """One thing with a date: an open task, or a note that carries a date property."""

    text: str
    path: str
    due: date | None = None
    time: str = ""
    tags: tuple[str, ...] = ()
    priority: int = NORMAL_PRIORITY
    kind: str = "task"  # task | note
    recurring: bool = False

    @property
    def label(self) -> str:
        return f"{self.time} {self.text}".strip() if self.time else self.text


@dataclass
class Agenda:
    overdue: list[Item] = field(default_factory=list)
    today: list[Item] = field(default_factory=list)
    tomorrow: list[Item] = field(default_factory=list)
    events: list[Item] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.overdue or self.today or self.tomorrow or self.events)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _item(text: str, path: str) -> Item:
    due = _DUE.search(text)
    clean = _DUE.sub("", text)
    clean = _DONE_DATE.sub("", clean)
    time = ""
    at = _TIME.match(clean.strip())
    if at and int(at.group(1)) < 24 and int(at.group(2)) < 60:
        time = f"{int(at.group(1)):02d}:{at.group(2)}"
        clean = clean.strip()[at.end():]
    priority = next((p for mark, p in _PRIORITY.items() if mark in clean), NORMAL_PRIORITY)
    # Tags are kept as data but dropped from the text: a digest line reads "pay the bills",
    # not "pay the bills #home #countdown" — the section above it already says where it sits.
    clean = _TAG.sub("", clean)
    return Item(
        recurring="🔁" in text,
        text=" ".join(clean.split()),
        path=path,
        due=_as_date(due.group(1)) if due else None,
        time=time,
        tags=tuple(_TAG.findall(text)),
        priority=priority,
    )


def open_tasks(index: VaultIndex) -> list[Item]:
    """Every open tick box in the vault, with its due date and time if it has one."""
    out: list[Item] = []
    for note in index.notes:
        if note.name == texts.VAULT_ARCHIVE_NOTE or note.name.startswith("_"):
            continue
        try:
            text = index.read(note.path)
        except OSError:
            continue
        if "- [" not in text:
            continue
        for _, mark, line in task_lines(text):
            if mark == OPEN:
                out.append(_item(line, note.path))
    return out


def dated_notes(index: VaultIndex, props: tuple[str, ...] = DATE_PROPS) -> list[Item]:
    """Notes that carry a date property: meetings, trips, anything with a day of its own."""
    out: list[Item] = []
    for note in index.notes:
        for prop in props:
            when = _as_date(note.props.get(prop))
            if when is not None:
                out.append(Item(text=note.name, path=note.path, due=when, kind="note"))
                break
    return out


def _sorted(items: list[Item]) -> list[Item]:
    return sorted(items, key=lambda i: (i.due or date.max, i.time or "99:99", i.priority,
                                        i.text.casefold()))


def build(index: VaultIndex, today: date, props: tuple[str, ...] = DATE_PROPS) -> Agenda:
    """Overdue, today and tomorrow — the shape of the morning message."""
    tomorrow = today + timedelta(days=1)
    tasks = open_tasks(index)
    agenda = Agenda(
        overdue=_sorted([t for t in tasks if t.due and t.due < today])[:MAX_PER_SECTION],
        today=_sorted([t for t in tasks if t.due == today])[:MAX_PER_SECTION],
        tomorrow=_sorted([t for t in tasks if t.due == tomorrow])[:MAX_PER_SECTION],
        events=_sorted([n for n in dated_notes(index, props)
                        if today <= (n.due or date.max) <= tomorrow])[:MAX_PER_SECTION],
    )
    return agenda


def on_day(index: VaultIndex, start: date, end: date | None = None,
           props: tuple[str, ...] = DATE_PROPS) -> list[Item]:
    """Everything dated inside a range: one day, a week, whatever the question named."""
    end = end or start
    both = open_tasks(index) + dated_notes(index, props)
    return _sorted([i for i in both if i.due and start <= i.due <= end])


def suggest(index: VaultIndex, today: date, limit: int = MAX_SUGGESTIONS) -> list[Item]:
    """What to do now: what is already late, oldest first, then what is due today, then the
    small pile of undated work — never more than a handful, because a list of forty is what
    the user is avoiding in the first place."""
    tasks = open_tasks(index)
    # A missed one-off is worth surfacing; a repeating chore whose date has slipped ("brush
    # teeth 🔁 every day") is late by nature and would otherwise fill the whole list.
    overdue = [t for t in tasks if t.due and t.due < today]
    late = (_sorted([t for t in overdue if not t.recurring])
            + _sorted([t for t in overdue if t.recurring]))
    now = _sorted([t for t in tasks if t.due == today])
    undated = sorted([t for t in tasks if t.due is None],
                     key=lambda t: (t.priority, t.text.casefold()))
    picked: list[Item] = []
    for group in (late, now, undated):
        for item in group:
            if len(picked) >= limit:
                return picked
            picked.append(item)
    return picked


# ---- rendering ---------------------------------------------------------------------------

def _lines(items: list[Item]) -> str:
    return "\n".join(texts.VAULT_AGENDA_ITEM.format(text=i.label) for i in items)


def digest(agenda: Agenda, today: date) -> str:
    """The morning message. Empty when there is nothing to say — no "you have 0 tasks"."""
    if agenda.empty:
        return ""
    parts = [texts.VAULT_AGENDA_HEADER.format(date=today.strftime("%d.%m"))]
    for title, items in (
        (texts.VAULT_AGENDA_OVERDUE, agenda.overdue),
        (texts.VAULT_AGENDA_TODAY, agenda.today),
        (texts.VAULT_AGENDA_TOMORROW, agenda.tomorrow),
        (texts.VAULT_AGENDA_EVENTS, agenda.events),
    ):
        if items:
            parts.append(f"{title.format(n=len(items))}\n{_lines(items)}")
    return "\n\n".join(parts)


def listing(items: list[Item], header: str) -> str:
    return f"{header}\n{_lines(items)}" if items else texts.VAULT_SEARCH_EMPTY
