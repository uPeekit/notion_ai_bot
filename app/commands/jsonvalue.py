"""JSON-safe conversion for typed field values, shared by commands/builder.py and
validation/policy.py. It lives in its own module (rather than in commands/builder.py) because it
is a leaf: this file depends only on notion/snapshot.py and validation/semantic.py, and nothing
else in app.commands depends on it. validation/policy.py importing it therefore creates no import
cycle, even though app.commands.builder also imports it."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.notion.snapshot import Option
from app.validation.semantic import DateRange


def _iso(d: date | datetime) -> str:
    return d.isoformat()


def to_json_value(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, Option):
        return {"id": v.id, "name": v.name}
    if isinstance(v, list):
        return [to_json_value(x) for x in v]
    if isinstance(v, DateRange):
        return {"start": _iso(v.start), "end": _iso(v.end) if v.end else None}
    return v
