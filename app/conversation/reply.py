"""Transport-neutral reply model: plain text plus button rows, built from the existing
Decision/Executor models. Nothing here imports python-telegram-bot (Plan 3b) or
app.conversation.session (Task 2, and importing it would be a cycle since session.py will
build its answers from these formatters) — format_question takes plain (option_id, label)
pairs instead of a storage model."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from app import texts
from app.commands.executor import ExecutionResult, Written
from app.commands.models import AppendBlocks, CreateItem, CreatePage, UpdateItem
from app.notion.snapshot import Option
from app.validation.policy import QType, Question
from app.validation.semantic import DateRange

SEARCH_LIMIT = 20

# Type-specific extra buttons, appended to the trailing row before BTN_CANCEL/BTN_INBOX. Empty
# for question types the user answers with free text (field_required without options,
# content_required, nothing_to_write) or with just their option buttons (target, item,
# field_ambiguous, field_required with options).
_EXTRAS: dict[QType, list[tuple[str, str]]] = {
    "target": [("other", texts.BTN_OTHER)],
    "intent_confirm": [("confirm", texts.BTN_CONFIRM), ("other", texts.BTN_OTHER)],
    "item": [],
    "item_not_found": [("add_new", texts.BTN_ADD_NEW)],
    "field_required": [],
    "field_ambiguous": [],
    "date": [("confirm", texts.BTN_CONFIRM), ("other", texts.BTN_OTHER)],
    "field_confirm": [("confirm", texts.BTN_CONFIRM), ("other", texts.BTN_OTHER)],
    "content_required": [],
    "nothing_to_write": [],
    "clarify": [],  # answered in free text
}


@dataclass(frozen=True)
class Button:
    id: str
    label: str


@dataclass(frozen=True)
class Reply:
    text: str
    buttons: list[list[Button]] = field(default_factory=list)
    undo_id: int | None = None


def _one_date(d: date | datetime) -> str:
    return d.strftime("%d.%m.%Y %H:%M") if isinstance(d, datetime) else d.strftime("%d.%m.%Y")


def _date_range_label(start: str, end: str | None, granularity: str) -> str:
    parse = datetime.fromisoformat if granularity == "datetime" else date.fromisoformat
    label = _one_date(parse(start))
    return f"{label} – {_one_date(parse(end))}" if end else label


def _value_label(v: Any) -> str:
    """Renders a typed Written.value (as stored on ExecutionResult) for a `• field: value` line.
    None means the field was cleared (explicit_null): select/status/date clears still produce a
    Written entry with value=None (see commands/builder.py and notion/mapper.py), so this must
    not fall through to str(None) == "None"."""
    if v is None:
        return texts.FIELD_CLEARED
    if isinstance(v, bool):
        return texts.BOOL_YES if v else texts.BOOL_NO
    if isinstance(v, Option):
        return v.name
    if isinstance(v, dict):
        # ExecutionResult.written carries the JSON-safe value the command was built from
        # (commands/builder.py runs every value through to_json_value), so an option arrives as
        # {"id","name"} and a date as {"start","end"} — not as the typed Option/DateRange.
        return _proposed_label(v)
    if isinstance(v, list):
        return ", ".join(_value_label(x) for x in v)
    if isinstance(v, DateRange):
        return _date_range_label(v.start.isoformat(), v.end.isoformat() if v.end else None,
                                 "datetime" if isinstance(v.start, datetime) else "date")
    return str(v)


def _proposed_label(v: Any) -> str:
    """Renders a JSON-safe Question.proposed value (Policy._proposed_json / to_json_value shapes:
    an Option as {"id","name"}, a DateRange as {"start","end","granularity"}, a list of either,
    or a bare scalar) for a question's text."""
    if isinstance(v, bool):
        return texts.BOOL_YES if v else texts.BOOL_NO
    if isinstance(v, list):
        return ", ".join(_proposed_label(x) for x in v)
    if isinstance(v, dict):
        if "name" in v:
            return str(v["name"])
        if "start" in v:
            granularity = v.get("granularity") or ("datetime" if "T" in str(v["start"]) else "date")
            return _date_range_label(v["start"], v.get("end"), granularity)
    return str(v)


def _question_text(q: Question, target_name: str | None) -> str:
    template = texts.QUESTION[q.type]
    if target_name is not None and q.type in texts.QUESTION_WITH_TARGET:
        template = texts.QUESTION_WITH_TARGET[q.type]
    kwargs: dict[str, Any] = {"target_name": target_name, "field_name": q.field_name}
    if q.type in ("intent_confirm", "target"):
        # A target question persisted before `proposed` carried the intent has None here.
        kwargs["intent"] = texts.INTENT_LABELS.get(q.proposed, texts.INTENT_UNKNOWN_LABEL)
    elif q.type in ("date", "field_confirm"):
        kwargs["value"] = _proposed_label(q.proposed)
    elif q.type == "item_not_found":
        kwargs["item_text"] = q.proposed
    elif q.type == "clarify":
        kwargs["question"] = q.proposed
    return template.format(**kwargs)


def format_question(
    q: Question, options: Sequence[tuple[str, str]], token: str, *, inbox: bool,
    target_name: str | None = None,
) -> Reply:
    rows = [[Button(f"a:{token}:{opt_id}", label)] for opt_id, label in options]
    trailing = [Button(f"a:{token}:{eid}", elabel) for eid, elabel in _EXTRAS[q.type]]
    trailing.append(Button(f"a:{token}:cancel", texts.BTN_CANCEL))
    if inbox:
        trailing.append(Button(f"a:{token}:inbox", texts.BTN_INBOX))
    rows.append(trailing)
    return Reply(text=_question_text(q, target_name), buttons=rows)


def _title_property_name(cmd: CreateItem) -> str | None:
    return next((p.property_name for p in cmd.properties if p.type == "title"), None)


def format_execution(result: ExecutionResult, *, target_url: str | None) -> str:
    cmd = result.command
    bullets: list[Written]
    if isinstance(cmd, CreateItem):
        title_name = _title_property_name(cmd)
        item_title = next((w.value for w in result.written if w.name == title_name),
                          cmd.target_name)
        header = texts.DONE_CREATE_ITEM.format(target_name=cmd.target_name, item_title=item_title)
        bullets = [w for w in result.written if w.name != title_name]
    elif isinstance(cmd, UpdateItem):
        header = texts.DONE_UPDATE.format(target_name=cmd.target_name, item_title=cmd.item_title)
        bullets = list(result.written)
    elif isinstance(cmd, CreatePage):
        header = texts.DONE_CREATE_PAGE.format(target_name=cmd.target_name, item_title=cmd.title)
        bullets = []
    elif isinstance(cmd, AppendBlocks):
        header = texts.DONE_APPEND.format(target_name=cmd.target_name, item_title=cmd.page_title)
        bullets = []
    else:
        raise TypeError(f"format_execution does not support {type(cmd).__name__}")

    lines = [header] + [f"• {w.name}: {_value_label(w.value)}" for w in bullets]
    url = result.url or target_url
    if url:
        lines.append(texts.DONE_LINK.format(url=url))
    return "\n".join(lines)


def format_search(result: ExecutionResult) -> str:
    hits = result.hits
    if not hits:
        return texts.SEARCH_EMPTY
    lines = [texts.SEARCH_HEADER]
    lines += [f"{i}. {h.title} — {h.url}" for i, h in enumerate(hits[:SEARCH_LIMIT], start=1)]
    return "\n".join(lines)
