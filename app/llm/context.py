from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from app.notion.snapshot import Field, Target, WorkspaceSnapshot

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
PAGE_TITLE_FIELD_ID = "__title__"
OPTION_TYPES = frozenset({"select", "status", "multi_select", "relation"})
_PAGE_TITLE = Field(
    id=PAGE_TITLE_FIELD_ID, name="Заголовок", type="title", required=True, options=[],
    relation_data_source_id=None, description="Название новой подстраницы",
)


def build_calendar(now: datetime) -> dict:
    """Pre-computed relative dates so the model looks them up instead of doing date math."""
    today = now.date()

    def label(d):
        return f"{d.isoformat()} ({RU_WEEKDAYS[d.weekday()]})"

    nearest = {}
    for i in range(1, 8):
        d = today + timedelta(days=i)
        nearest[RU_WEEKDAYS[d.weekday()]] = d.isoformat()
    days_to_next_monday = 7 - today.weekday()
    next_monday = today + timedelta(days=days_to_next_monday)
    next_sunday = next_monday + timedelta(days=6)
    return {
        "сегодня": label(today),
        "завтра": label(today + timedelta(days=1)),
        "послезавтра": label(today + timedelta(days=2)),
        "ближайшие дни": nearest,
        "через неделю": (today + timedelta(days=7)).isoformat(),
        "через две недели": (today + timedelta(days=14)).isoformat(),
        "следующая неделя": f"{next_monday.isoformat()} … {next_sunday.isoformat()}",
    }


@dataclass(frozen=True)
class KeyRef:
    kind: Literal["target", "field", "option", "item"]
    target_id: str
    name: str
    field_id: str | None = None
    field_type: str | None = None
    option_id: str | None = None
    item_id: str | None = None


@dataclass
class Context:
    payload: dict
    keys: dict[str, KeyRef]
    now: datetime
    pending: dict | None = None
    fields_by_target: dict[str, list[str]] = field(default_factory=dict)
    options_by_field: dict[str, list[str]] = field(default_factory=dict)
    items_by_target: dict[str, list[str]] = field(default_factory=dict)

    def json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))

    def ref(self, key: str) -> KeyRef | None:
        return self.keys.get(key)

    def target_keys(self) -> list[str]:
        return [k for k, r in self.keys.items() if r.kind == "target"]

    def target_key(self, target_id: str) -> str | None:
        for k, r in self.keys.items():
            if r.kind == "target" and r.target_id == target_id:
                return k
        return None

    def field_keys(self, target_key: str) -> list[str]:
        return list(self.fields_by_target.get(target_key, []))

    def option_keys(self, field_key: str) -> list[str]:
        return list(self.options_by_field.get(field_key, []))

    def item_keys(self, target_key: str) -> list[str]:
        return list(self.items_by_target.get(target_key, []))

    def field_key(self, target_id: str, field_id: str) -> str | None:
        """Reverse lookup: Notion field id -> context key (for Plan 3, after rebuild)."""
        for k, r in self.keys.items():
            if r.kind == "field" and r.target_id == target_id and r.field_id == field_id:
                return k
        return None

    def option_key(self, target_id: str, field_id: str, option_id: str) -> str | None:
        for k, r in self.keys.items():
            if (r.kind == "option" and r.target_id == target_id and r.field_id == field_id
                    and r.option_id == option_id):
                return k
        return None

    def item_key(self, target_id: str, item_id: str) -> str | None:
        for k, r in self.keys.items():
            if r.kind == "item" and r.target_id == target_id and r.item_id == item_id:
                return k
        return None


class ContextBuilder:
    def __init__(self, timezone: str = "Europe/Tallinn", items_per_target: int = 50) -> None:
        self._tz = ZoneInfo(timezone)
        self._items_per_target = items_per_target

    def build(
        self, snapshot: WorkspaceSnapshot, now: datetime | None = None, pending: dict | None = None
    ) -> Context:
        if now is not None and now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        now = (now or datetime.now(self._tz)).astimezone(self._tz)
        ctx = Context(payload={}, keys={}, now=now, pending=pending)
        targets = []
        for ti, t in enumerate(snapshot.targets, start=1):
            tk = f"t{ti}"
            ctx.keys[tk] = KeyRef("target", t.id, t.name)
            entry: dict = {
                "key": tk, "kind": t.kind, "name": t.name, "path": t.path,
                "ops": sorted(op.replace("create_page", "create") for op in t.operations),
            }
            if t.description:
                entry["description"] = t.description
            entry["fields"] = self._fields(ctx, tk, t)
            items: dict[str, str] = {}
            for ii, it in enumerate(t.items[: self._items_per_target], start=1):
                ik = f"{tk}.i{ii}"
                ctx.keys[ik] = KeyRef("item", t.id, it.title, item_id=it.id)
                items[ik] = f"{it.title} ({it.hint})" if it.hint else it.title
            ctx.items_by_target[tk] = list(items)
            entry["items" if t.kind == "database" else "children"] = items
            targets.append(entry)
        ctx.payload = {
            "now": now.isoformat(timespec="minutes"),
            "tz": str(self._tz),
            "weekday": RU_WEEKDAYS[now.weekday()],
            "calendar": build_calendar(now),
            "targets": targets,
        }
        if pending:
            ctx.payload["pending"] = pending
        return ctx

    @staticmethod
    def _fields(ctx: Context, tk: str, t: Target) -> list[dict]:
        source = list(t.fields) if t.kind == "database" else [_PAGE_TITLE]
        out: list[dict] = []
        n = 0
        for f in source:
            if not f.writable or (f.type in OPTION_TYPES and not f.options):
                continue
            n += 1
            fk = f"{tk}.f{n}"
            ctx.keys[fk] = KeyRef("field", t.id, f.name, field_id=f.id, field_type=f.type)
            entry: dict = {"key": fk, "name": f.name, "type": f.type}
            if f.required:
                entry["required"] = True
            if f.description:
                entry["description"] = f.description
            if f.type in OPTION_TYPES:
                opts: dict[str, str] = {}
                for oi, o in enumerate(f.options, start=1):
                    ok = f"{fk}.o{oi}"
                    ctx.keys[ok] = KeyRef(
                        "option", t.id, o.name, field_id=f.id, field_type=f.type, option_id=o.id
                    )
                    opts[ok] = o.name
                ctx.options_by_field[fk] = list(opts)
                entry["options"] = opts
            out.append(entry)
        ctx.fields_by_target[tk] = [e["key"] for e in out]
        return out
