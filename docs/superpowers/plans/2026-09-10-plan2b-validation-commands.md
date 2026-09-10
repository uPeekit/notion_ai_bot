# Plan 2b: Semantic Validation, Policy, Commands, Mapper, Executor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a validated LLM `Interpretation` into a deterministic decision (EXECUTE / CLARIFY / REJECT) and, on EXECUTE, into a typed application command that the executor turns into Notion API calls with an undo record.

**Architecture:** `SemanticValidator` re-checks every key against the request `Context` and `WorkspaceSnapshot`, types every value per field type, dedupes candidates by target, and produces `ValidationResult`. `Policy` applies configurable thresholds and emits a `Decision` with an ordered list of structured `Question`s (Plan 3 renders them). `CommandBuilder` turns the chosen candidate into a `Command` (Pydantic, JSON-serialisable for the audit log). `mapper.py` is the only place that builds Notion JSON. `Executor` runs commands through `NotionProvider`, collects what was written, and builds `UndoRecord`s (archive / restore previous properties / delete blocks).

**Tech Stack:** Python 3.12, pydantic 2, existing `NotionProvider` + `FakeNotionProvider`, `tools/sample_workspace.py`, `ContextBuilder`.

**Spec:** `documentation/ARCHITECTURE.md` §7–§9, §14; `documentation/DATA_MODEL.md` §4–§5; `documentation/ERRORS.md` (`SEM_*` codes); `documentation/FLOWS.md` F1–F12; IMPLEMENTATION_PLAN T-030…T-034. Plan 2a final review rulings: the validator must dedupe candidates by target, type values per field, cap `item_candidates` (8) and text lengths.

## Global Constraints

- The validator trusts nothing from the LLM: every target/field/option/item reference must resolve through `Context.ref()` and belong to the candidate's target; values are typed per `KeyRef.field_type`; unknown keys or type errors invalidate that candidate (`SEM_UNKNOWN_KEY`, `SEM_TYPE`), never crash.
- Notion ids appear only after validation (in `VCandidate` / `Command`), never in anything sent back to the LLM.
- Thresholds come from `Settings` (`policy_intent_min` 0.85, `policy_target_min` 0.85, `policy_target_margin` 0.10, `policy_field_min` 0.75, `policy_date_min` 0.80); no hard-coded numbers in `policy.py`.
- Only `notion/mapper.py` builds Notion JSON; `executor.py` calls `NotionProvider` only.
- Property writes are keyed by Notion property id; select/status/multi_select by option id; relation by page id.
- `explicit_null` clears a property (select→`None`, multi_select/relation→`[]`, rich_text→`[]`, date→`None`, number/url→`None`, checkbox→`False`); `status` cannot be cleared by the Notion API — an `explicit_null` on a status field is dropped with an issue.
- Risk classes: `create_item`, `create_page`, `append_blocks`, `search` = LOW; `update_item` = MEDIUM; all auto-execute (user decision), risk recorded on the `Decision` for the audit log.
- `uv run ruff check .` and `uv run pytest -q` pass before each commit; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; named `git add` only. PowerShell; uv may be at `$env:USERPROFILE\.local\bin\uv.exe`.

## File structure

```text
app/validation/__init__.py
app/validation/semantic.py     Issue, DateRange, VField, VCandidate, ValidationResult, SemanticValidator
app/validation/policy.py       Thresholds, QOption, Question, Decision, Policy
app/commands/__init__.py
app/commands/models.py         PropertyWrite, CreateItem, UpdateItem, CreatePage, AppendBlocks, Search, Command, RISK
app/commands/builder.py        build_command(), paragraphs()
app/notion/mapper.py           property_payload(), properties_payload(), create_item_payload(), create_page_payload(),
                               paragraph_blocks(), search_filter(), read_to_write()
app/commands/executor.py       Written, SearchHit, UndoRecord, ExecutionResult, Executor
tests/helpers.py               make_interp() / ctx_and_snapshot() helpers shared by the new tests
tests/test_semantic.py, tests/test_policy.py, tests/test_commands.py, tests/test_mapper.py, tests/test_executor.py
```

---

### Task 1: Semantic validator

**Files:**
- Create: `app/validation/__init__.py` (empty), `app/validation/semantic.py`, `tests/helpers.py`, `tests/test_semantic.py`

**Interfaces:**
- Consumes: `Interpretation`, `Candidate`, `Value`, `Ambiguous`, `NotMentioned`, `ExplicitNull` (`app.interpretation.models`); `Context`, `KeyRef`, `PAGE_TITLE_FIELD_ID` (`app.llm.context`); `Target`, `Field`, `Option`, `Item`, `WorkspaceSnapshot` (`app.notion.snapshot`).
- Produces:
  - `Issue(code: str, message: str, key: str | None = None)` frozen; codes `INTENT_UNKNOWN`, `SEM_UNKNOWN_KEY`, `SEM_TYPE`, `SEM_UNSUPPORTED_OP`, `SEM_READONLY_FIELD`, `SEM_STATUS_CLEAR`.
  - `DateRange(start: date | datetime, end: date | datetime | None = None)` frozen.
  - `VField(key, ref, field, status, value=None, candidates=[], confidence=1.0, source_text="")` — `value` typed: `str` (title/rich_text/url), `float|int` (number), `bool` (checkbox), `DateRange` (date), `Option` (select/status), `list[Option]` (multi_select/relation).
  - `VCandidate(key, target: Target, confidence, item: Item | None, item_candidates: list[Item], fields: dict[str, VField], content: str | None, search_query: str | None)`.
  - `ValidationResult(intent: str, intent_confidence: float, candidates: list[VCandidate], issues: list[Issue])` with properties `rejected`, `best`, `second`.
  - `class SemanticValidator` with `validate(interp: Interpretation, ctx: Context, snapshot: WorkspaceSnapshot) -> ValidationResult`; constants `MAX_ITEM_CANDIDATES = 8`, `MAX_TEXT = 4000`, `INTENT_OPS`.

- [ ] **Step 1: `tests/helpers.py`**

```python
"""Builders shared by validation/command/executor tests."""

from __future__ import annotations

from typing import Any

from app.interpretation.models import Interpretation
from app.llm.context import Context, ContextBuilder
from app.notion.snapshot import WorkspaceSnapshot
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def ctx_and_snapshot() -> tuple[Context, WorkspaceSnapshot]:
    snap = sample_snapshot()
    return ContextBuilder("Europe/Tallinn").build(snap, now=SAMPLE_NOW), snap


def val(v: Any, conf: float = 0.9, src: str = "") -> dict:
    return {"status": "value", "value": v, "confidence": conf, "source_text": src}


def amb(*cands: Any, src: str = "") -> dict:
    return {"status": "ambiguous", "candidates": list(cands), "source_text": src}


def cand(ctx: Context, target: str, conf: float = 0.9, *, item: str | None = None,
         item_candidates: list[str] | None = None, fields: dict | None = None,
         content: str | None = None, search_query: str | None = None) -> dict:
    base = {k: {"status": "not_mentioned"} for k in ctx.field_keys(target)}
    base.update(fields or {})
    return {"target": target, "confidence": conf, "item": item,
            "item_candidates": item_candidates or [], "fields": base,
            "content": content, "search_query": search_query}


def make_interp(intent: str, *cands: dict, intent_conf: float = 0.95) -> Interpretation:
    return Interpretation.model_validate(
        {"intent": {"value": intent, "confidence": intent_conf}, "candidates": list(cands), "notes": ""}
    )
```

Sample key map (from `tools/sample_workspace.py` via `ContextBuilder`): `t1` Дом (page, no children), `t2` Покупки (`t2.f1` Название title, `t2.f2` Магазин select o1 Rimi/o2 Prisma/o3 Maxima, `t2.f3` Категория select, `t2.f4` Количество number, `t2.f5` Куплено checkbox, `t2.f6` Заметка rich_text; items `t2.i1` Хлеб, `t2.i2` Молоко, `t2.i3` Яйца, `t2.i4` Молоко овсяное), `t3` Задачи (`t3.f1` Задача title, `t3.f2` Приоритет select required A/B/C, `t3.f3` Срок date, `t3.f4` Статус status To do/Doing/Done, `t3.f5` Проект relation Дом/Работа, `t3.f6` Теги multi_select, `t3.f7` Ссылка url; items `t3.i1` Подготовить документы, `t3.i2` Позвонить маме, `t3.i3` Купить билеты), `t4` Проекты, `t5` Идеи (page, `t5.f1` Заголовок, children `t5.i1` Отпуск 2027, `t5.i2` Книги).

- [ ] **Step 2: failing tests** — `tests/test_semantic.py`:

```python
from datetime import date, datetime

from app.notion.snapshot import Option
from app.validation.semantic import DateRange, SemanticValidator
from tests.helpers import amb, cand, ctx_and_snapshot, make_interp, val

V = SemanticValidator()


def run(interp):
    ctx, snap = ctx_and_snapshot()
    return V.validate(interp, ctx, snap), ctx


def test_happy_create_types_values():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t2", fields={
        "t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1"), "t2.f4": val(2), "t2.f5": val(True),
        "t2.f6": val("зерновой")}))
    r, _ = run(interp)
    assert not r.rejected and r.issues == []
    c = r.best
    assert c.target.name == "Покупки" and c.key == "t2"
    assert c.fields["t2.f1"].value == "Молоко" and c.fields["t2.f1"].field.type == "title"
    assert c.fields["t2.f2"].value == Option("o-Rimi", "Rimi")
    assert c.fields["t2.f4"].value == 2 and c.fields["t2.f5"].value is True
    assert c.fields["t2.f3"].status == "not_mentioned" and c.fields["t2.f3"].value is None


def test_date_multi_relation_url_typing():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t3", fields={
        "t3.f1": val("Позвонить"), "t3.f3": val({"start": "2026-09-10", "end": None}),
        "t3.f5": val(["t3.f5.o2"]), "t3.f6": val(["t3.f6.o1", "t3.f6.o3", "t3.f6.o1"]),
        "t3.f7": val("https://example.com/a")}))
    r, _ = run(interp)
    c = r.best
    assert c.fields["t3.f3"].value == DateRange(date(2026, 9, 10), None)
    assert [o.name for o in c.fields["t3.f5"].value] == ["Работа"]
    assert [o.name for o in c.fields["t3.f6"].value] == ["дом", "здоровье"]  # deduped
    assert c.fields["t3.f7"].value == "https://example.com/a"


def test_datetime_gets_context_timezone():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t3", fields={
        "t3.f1": val("x"), "t3.f3": val({"start": "2026-09-10T09:45", "end": "2026-09-10T10:30"})}))
    r, _ = run(interp)
    d = r.best.fields["t3.f3"].value
    assert isinstance(d.start, datetime) and d.start.tzinfo is not None
    assert d.start.isoformat() == "2026-09-10T09:45:00+03:00" and d.end.hour == 10


def test_type_errors_invalidate_candidate():
    ctx, _ = ctx_and_snapshot()
    bad = [
        {"t2.f4": val("two")},                                   # number
        {"t2.f5": val("yes")},                                   # checkbox
        {"t3.f3": val({"start": "завтра", "end": None})},        # date
        {"t3.f3": val({"start": "2026-09-12", "end": "2026-09-10"})},  # end < start
        {"t3.f7": val("example.com")},                           # url
        {"t2.f2": val("t2.f3.o1")},                              # option of another field
        {"t2.f2": val("Rimi")},                                  # option by name
    ]
    for fields in bad:
        target = next(iter(fields)).split(".")[0]
        r, _ = run(make_interp("create", cand(ctx, target, fields={"t2.f1" if target == "t2" else "t3.f1": val("x"), **fields})))
        assert r.rejected, fields
        assert any(i.code == "SEM_TYPE" for i in r.issues), fields


def test_unknown_keys_invalidate_candidate():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", {**cand(ctx, "t2"), "target": "t9"}))
    assert r.rejected and r.issues[0].code == "SEM_UNKNOWN_KEY"
    c = cand(ctx, "t2")
    c["fields"]["t3.f1"] = val("x")
    r, _ = run(make_interp("create", c))
    assert r.rejected and any(i.code == "SEM_UNKNOWN_KEY" for i in r.issues)
    r, _ = run(make_interp("update", cand(ctx, "t2", item="t3.i1")))
    assert r.rejected and any(i.code == "SEM_UNKNOWN_KEY" for i in r.issues)


def test_missing_field_key_treated_as_not_mentioned():
    ctx, _ = ctx_and_snapshot()
    c = cand(ctx, "t2", fields={"t2.f1": val("x")})
    del c["fields"]["t2.f6"]
    r, _ = run(make_interp("create", c))
    assert not r.rejected and r.best.fields["t2.f6"].status == "not_mentioned"


def test_unsupported_operation():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("append", cand(ctx, "t2", item="t2.i1", content="x")))   # db has no append
    assert r.rejected and r.issues[0].code == "SEM_UNSUPPORTED_OP"
    r, _ = run(make_interp("update", cand(ctx, "t5", item="t5.i1")))               # page has no update
    assert r.rejected


def test_intent_unknown_rejects():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("unknown", cand(ctx, "t2")))
    assert r.rejected and r.issues[0].code == "INTENT_UNKNOWN"


def test_dedupe_and_sort_candidates():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", cand(ctx, "t2", 0.6), cand(ctx, "t3", 0.8), cand(ctx, "t2", 0.7)))
    assert [(c.key, c.confidence) for c in r.candidates] == [("t3", 0.8), ("t2", 0.7)]
    assert r.best.key == "t3" and r.second.key == "t2"


def test_items_and_candidates_resolved_and_capped():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("update", cand(ctx, "t2", item=None,
                                          item_candidates=["t2.i2", "t2.i4", "t2.i2"])))
    c = r.best
    assert c.item is None and [i.title for i in c.item_candidates] == ["Молоко", "Молоко овсяное"]
    r, _ = run(make_interp("update", cand(ctx, "t2", item="t2.i2", fields={"t2.f5": val(True)})))
    assert r.best.item.id == "b-milk"


def test_ambiguous_candidates_typed():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", cand(ctx, "t2", fields={
        "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2", "bogus")})))
    f = r.best.fields["t2.f2"]
    assert f.status == "ambiguous" and [o.name for o in f.candidates] == ["Rimi", "Prisma"]
    r, _ = run(make_interp("create", cand(ctx, "t2", fields={"t2.f1": val("Сыр"), "t2.f2": amb("bogus")})))
    assert r.best.fields["t2.f2"].status == "not_mentioned"


def test_status_explicit_null_dropped_with_issue():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("update", cand(ctx, "t3", item="t3.i1", fields={"t3.f4": {"status": "explicit_null"}})))
    assert not r.rejected
    assert r.best.fields["t3.f4"].status == "not_mentioned"
    assert any(i.code == "SEM_STATUS_CLEAR" for i in r.issues)


def test_page_title_field_and_text_caps():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", cand(ctx, "t5", fields={"t5.f1": val("Отпуск 2028")}, content="x" * 5000)))
    c = r.best
    assert c.fields["t5.f1"].field.id == "__title__" and c.fields["t5.f1"].value == "Отпуск 2028"
    assert len(c.content) == 4000
```

- [ ] **Step 3:** run → ModuleNotFoundError.

- [ ] **Step 4: `app/validation/semantic.py`**

```python
"""Deterministic re-check of the LLM interpretation against the request context and snapshot."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal

from app.interpretation.models import Ambiguous, Candidate, ExplicitNull, Interpretation, Value
from app.llm.context import Context, KeyRef
from app.notion.snapshot import Field, Item, Option, Target, WorkspaceSnapshot

MAX_ITEM_CANDIDATES = 8
MAX_TEXT = 4000
INTENT_OPS: dict[str, frozenset[str]] = {
    "create": frozenset({"create", "create_page"}),
    "update": frozenset({"update"}),
    "append": frozenset({"append"}),
    "search": frozenset({"search"}),
}
Status = Literal["not_mentioned", "explicit_null", "ambiguous", "value"]


class TypeError_(ValueError):
    """Raised internally when an LLM value does not fit the field type."""


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    key: str | None = None


@dataclass(frozen=True)
class DateRange:
    start: date | datetime
    end: date | datetime | None = None


@dataclass
class VField:
    key: str
    ref: KeyRef
    field: Field
    status: Status
    value: Any = None
    candidates: list[Any] = field(default_factory=list)
    confidence: float = 1.0
    source_text: str = ""


@dataclass
class VCandidate:
    key: str
    target: Target
    confidence: float
    item: Item | None
    item_candidates: list[Item]
    fields: dict[str, VField]
    content: str | None
    search_query: str | None


@dataclass
class ValidationResult:
    intent: str
    intent_confidence: float
    candidates: list[VCandidate]
    issues: list[Issue]

    @property
    def rejected(self) -> bool:
        return not self.candidates

    @property
    def best(self) -> VCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def second(self) -> VCandidate | None:
        return self.candidates[1] if len(self.candidates) > 1 else None


def _parse_when(raw: Any, ctx: Context) -> date | datetime:
    if not isinstance(raw, str) or not raw:
        raise TypeError_("date must be a string")
    try:
        if "T" in raw:
            dt = datetime.fromisoformat(raw)
            return dt if dt.tzinfo else dt.replace(tzinfo=ctx.now.tzinfo)
        return date.fromisoformat(raw)
    except ValueError as e:
        raise TypeError_(f"bad date {raw!r}") from e


def _cmp_key(d: date | datetime) -> datetime:
    if isinstance(d, datetime):
        return d
    return datetime(d.year, d.month, d.day, tzinfo=None)


def _option(ctx: Context, field_key: str, raw: Any) -> Option:
    if not isinstance(raw, str) or raw not in ctx.option_keys(field_key):
        raise TypeError_(f"{raw!r} is not an option of {field_key}")
    ref = ctx.ref(raw)
    assert ref is not None and ref.option_id is not None
    return Option(ref.option_id, ref.name)


def typed_value(ctx: Context, field_key: str, ftype: str, raw: Any) -> Any:
    """Convert one raw LLM value to the typed python value for a field type."""
    if ftype in ("title", "rich_text"):
        if not isinstance(raw, str):
            raise TypeError_("text expected")
        return raw.strip()[:MAX_TEXT]
    if ftype == "url":
        if not isinstance(raw, str) or not raw.startswith(("http://", "https://")):
            raise TypeError_("url must start with http(s)://")
        return raw.strip()
    if ftype == "number":
        if isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(raw):
            raise TypeError_("number expected")
        return raw
    if ftype == "checkbox":
        if not isinstance(raw, bool):
            raise TypeError_("boolean expected")
        return raw
    if ftype == "date":
        if not isinstance(raw, dict):
            raise TypeError_("date object expected")
        start = _parse_when(raw.get("start"), ctx)
        end = _parse_when(raw["end"], ctx) if raw.get("end") else None
        if end is not None:
            a, b = _cmp_key(start), _cmp_key(end)
            if (a.tzinfo is None) != (b.tzinfo is None):
                a, b = a.replace(tzinfo=None), b.replace(tzinfo=None)
            if b < a:
                raise TypeError_("end before start")
        return DateRange(start, end)
    if ftype in ("select", "status"):
        return _option(ctx, field_key, raw)
    if ftype in ("multi_select", "relation"):
        if not isinstance(raw, list):
            raise TypeError_("list expected")
        out: list[Option] = []
        for x in raw:
            o = _option(ctx, field_key, x)
            if o not in out:
                out.append(o)
        return out
    raise TypeError_(f"unsupported field type {ftype}")


class SemanticValidator:
    def validate(
        self, interp: Interpretation, ctx: Context, snapshot: WorkspaceSnapshot
    ) -> ValidationResult:
        issues: list[Issue] = []
        intent = interp.intent.value
        result = ValidationResult(intent, interp.intent.confidence, [], issues)
        if intent == "unknown":
            issues.append(Issue("INTENT_UNKNOWN", "message is not a Notion request"))
            return result

        by_target: dict[str, VCandidate] = {}
        for cand in interp.candidates:
            vc = self._candidate(cand, intent, ctx, snapshot, issues)
            if vc is None:
                continue
            prev = by_target.get(vc.target.id)
            if prev is None or vc.confidence > prev.confidence:
                by_target[vc.target.id] = vc
        result.candidates = sorted(by_target.values(), key=lambda c: c.confidence, reverse=True)
        return result

    def _candidate(
        self, cand: Candidate, intent: str, ctx: Context, snapshot: WorkspaceSnapshot,
        issues: list[Issue],
    ) -> VCandidate | None:
        tref = ctx.ref(cand.target)
        if tref is None or tref.kind != "target":
            issues.append(Issue("SEM_UNKNOWN_KEY", f"unknown target {cand.target}", cand.target))
            return None
        target = snapshot.target(tref.target_id)
        if target is None:
            issues.append(Issue("SEM_UNKNOWN_KEY", f"target {cand.target} not in snapshot", cand.target))
            return None
        if not (INTENT_OPS[intent] & target.operations):
            issues.append(Issue("SEM_UNSUPPORTED_OP", f"{intent} not supported for {target.name}", cand.target))
            return None

        allowed = ctx.field_keys(cand.target)
        for fk in cand.fields:
            if fk not in allowed:
                issues.append(Issue("SEM_UNKNOWN_KEY", f"field {fk} not in {cand.target}", fk))
                return None
        fields: dict[str, VField] = {}
        for fk in allowed:
            ref = ctx.ref(fk)
            assert ref is not None and ref.field_id is not None and ref.field_type is not None
            fdef = self._field_def(target, ref)
            raw = cand.fields.get(fk)
            try:
                fields[fk] = self._field(fk, ref, fdef, raw, ctx, issues)
            except TypeError_ as e:
                issues.append(Issue("SEM_TYPE", f"{ref.name}: {e}", fk))
                return None

        item = None
        if cand.item is not None:
            item = self._item(cand.item, cand.target, target, ctx)
            if item is None:
                issues.append(Issue("SEM_UNKNOWN_KEY", f"item {cand.item} not in {cand.target}", cand.item))
                return None
        item_candidates: list[Item] = []
        for ik in cand.item_candidates:
            it = self._item(ik, cand.target, target, ctx)
            if it is None:
                issues.append(Issue("SEM_UNKNOWN_KEY", f"item {ik} not in {cand.target}", ik))
                return None
            if it not in item_candidates:
                item_candidates.append(it)
        item_candidates = item_candidates[:MAX_ITEM_CANDIDATES]

        content = cand.content.strip()[:MAX_TEXT] if cand.content else None
        query = cand.search_query.strip()[:MAX_TEXT] if cand.search_query else None
        return VCandidate(cand.target, target, cand.confidence, item, item_candidates, fields,
                          content or None, query or None)

    @staticmethod
    def _field_def(target: Target, ref: KeyRef) -> Field:
        fdef = target.field(ref.field_id or "")
        if fdef is None:  # synthetic page title
            fdef = Field(id=ref.field_id or "", name=ref.name, type="title", required=True,
                         options=[], relation_data_source_id=None, description="")
        return fdef

    @staticmethod
    def _item(key: str, target_key: str, target: Target, ctx: Context) -> Item | None:
        if key not in ctx.item_keys(target_key):
            return None
        ref = ctx.ref(key)
        return target.item(ref.item_id) if ref and ref.item_id else None

    @staticmethod
    def _field(fk: str, ref: KeyRef, fdef: Field, raw: Any, ctx: Context, issues: list[Issue]) -> VField:
        ftype = ref.field_type or fdef.type
        if raw is None or getattr(raw, "status", None) == "not_mentioned":
            return VField(fk, ref, fdef, "not_mentioned")
        if isinstance(raw, ExplicitNull):
            if ftype == "status":
                issues.append(Issue("SEM_STATUS_CLEAR", f"{ref.name}: status cannot be cleared", fk))
                return VField(fk, ref, fdef, "not_mentioned")
            return VField(fk, ref, fdef, "explicit_null")
        if isinstance(raw, Ambiguous):
            typed: list[Any] = []
            for c in raw.candidates:
                try:
                    v = typed_value(ctx, fk, ftype, c)
                except TypeError_:
                    continue
                if v not in typed:
                    typed.append(v)
            if not typed:
                return VField(fk, ref, fdef, "not_mentioned", source_text=raw.source_text)
            if len(typed) == 1:
                return VField(fk, ref, fdef, "value", typed[0], [], 0.5, raw.source_text)
            return VField(fk, ref, fdef, "ambiguous", None, typed, 0.0, raw.source_text)
        if isinstance(raw, Value):
            v = typed_value(ctx, fk, ftype, raw.value)
            return VField(fk, ref, fdef, "value", v, [], raw.confidence, raw.source_text)
        raise TypeError_(f"unexpected field payload {type(raw).__name__}")
```

- [ ] **Step 5:** tests pass (13). Ruff (rename `TypeError_` if ruff N-rules complain — they are not enabled; keep). Commit `feat: semantic validator (keys, typing, dedupe, caps)`.

---

### Task 2: Policy engine

**Files:**
- Create: `app/validation/policy.py`, `tests/test_policy.py`

**Interfaces:**
- Produces: `Thresholds(intent_min, target_min, target_margin, field_min, date_min)` frozen with `@classmethod from_settings(s: Settings)`; `QOption(key: str, label: str)`; `Question(type, target_key=None, field_key=None, field_name=None, options=[], proposed=None)` with `type` in `target | item | item_not_found | field_required | field_ambiguous | field_confirm | date | content_required`; `Decision(kind: "EXECUTE"|"CLARIFY"|"REJECT", candidate: VCandidate | None, questions: list[Question], reasons: list[str], risk: "LOW"|"MEDIUM"|None)`; `Policy(thresholds).evaluate(result: ValidationResult) -> Decision`; `RISK_BY_INTENT = {"create":"LOW","append":"LOW","search":"LOW","update":"MEDIUM"}`.
- Question ordering: target → item / item_not_found → field_required → field_ambiguous → date → field_confirm → content_required.

- [ ] **Step 1: failing tests** — `tests/test_policy.py`:

```python
import pytest

from app.config import Settings
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from tests.helpers import amb, cand, ctx_and_snapshot, make_interp, val

T = Thresholds(intent_min=0.85, target_min=0.85, target_margin=0.10, field_min=0.75, date_min=0.80)


def decide(interp, t=T):
    ctx, snap = ctx_and_snapshot()
    return Policy(t).evaluate(SemanticValidator().validate(interp, ctx, snap)), ctx


def test_thresholds_from_settings(env):
    t = Thresholds.from_settings(Settings(_env_file=None))
    assert t == T


def test_execute_simple_create():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("Молоко")})))
    assert d.kind == "EXECUTE" and d.candidate.key == "t2" and d.questions == [] and d.risk == "LOW"


def test_reject_when_validator_rejects():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("unknown", cand(ctx, "t2")))
    assert d.kind == "REJECT" and d.candidate is None and "INTENT_UNKNOWN" in d.reasons[0]


@pytest.mark.parametrize("best,second,expect", [
    (0.90, 0.79, "EXECUTE"), (0.90, 0.80, "EXECUTE"), (0.90, 0.81, "CLARIFY"),
    (0.89, None, "CLARIFY"), (0.85, None, "EXECUTE"), (0.84, None, "CLARIFY"),
])
def test_target_threshold_and_margin_boundaries(best, second, expect):
    ctx, _ = ctx_and_snapshot()
    cands = [cand(ctx, "t2", best, fields={"t2.f1": val("x")})]
    if second is not None:
        cands.append(cand(ctx, "t3", second, fields={"t3.f1": val("x"), "t3.f2": val("t3.f2.o1")}))
    d, _ = decide(make_interp("create", *cands))
    assert d.kind == expect
    if expect == "CLARIFY":
        q = d.questions[0]
        assert q.type == "target" and [o.key for o in q.options][0] == "t2"
        assert q.options[0].label == "Покупки"


def test_intent_confidence_boundary():
    ctx, _ = ctx_and_snapshot()
    ok = make_interp("create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("x")}), intent_conf=0.85)
    low = make_interp("create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("x")}), intent_conf=0.84)
    assert decide(ok)[0].kind == "EXECUTE"
    d, _ = decide(low)
    assert d.kind == "CLARIFY" and d.questions[0].type == "target"


def test_required_field_missing_asks_with_options():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={"t3.f1": val("Документы")})))
    assert d.kind == "CLARIFY"
    q = d.questions[0]
    assert q.type == "field_required" and q.field_key == "t3.f2" and q.field_name == "Приоритет"
    assert [o.label for o in q.options] == ["A", "B", "C"] and q.options[0].key == "t3.f2.o1"
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={"t3.f1": val("x"), "t3.f2": {"status": "explicit_null"}})))
    assert d.kind == "CLARIFY" and d.questions[0].type == "field_required"


def test_required_not_checked_for_update():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t3", 0.95, item="t3.i1", fields={"t3.f4": val("t3.f4.o3")})))
    assert d.kind == "EXECUTE" and d.risk == "MEDIUM"


def test_ambiguous_field_asks():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={
        "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2")})))
    q = d.questions[0]
    assert d.kind == "CLARIFY" and q.type == "field_ambiguous" and q.field_key == "t2.f2"
    assert [(o.key, o.label) for o in q.options] == [("t2.f2#0", "Rimi"), ("t2.f2#1", "Prisma")]


@pytest.mark.parametrize("conf,expect", [(0.80, "EXECUTE"), (0.79, "CLARIFY")])
def test_date_confidence_boundary(conf, expect):
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={
        "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"), "t3.f3": val({"start": "2026-09-14", "end": None}, conf)})))
    assert d.kind == expect
    if expect == "CLARIFY":
        assert d.questions[0].type == "date" and d.questions[0].proposed.start.isoformat() == "2026-09-14"


@pytest.mark.parametrize("conf,expect", [(0.75, "EXECUTE"), (0.74, "CLARIFY")])
def test_field_confidence_boundary(conf, expect):
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={
        "t2.f1": val("x"), "t2.f2": val("t2.f2.o1", conf)})))
    assert d.kind == expect
    if expect == "CLARIFY":
        assert d.questions[0].type == "field_confirm" and d.questions[0].proposed.name == "Rimi"


def test_update_item_resolution():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, item_candidates=["t2.i2", "t2.i4"], fields={"t2.f5": val(True)})))
    assert d.kind == "CLARIFY" and d.questions[0].type == "item"
    assert [o.label for o in d.questions[0].options] == ["Молоко", "Молоко овсяное"]
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, fields={"t2.f5": val(True)})))
    assert d.kind == "REJECT" and d.questions[0].type == "item_not_found" and d.candidate is not None


def test_append_to_page_itself_and_content_required():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("append", cand(ctx, "t5", 0.95, content="текст")))
    assert d.kind == "EXECUTE" and d.candidate.item is None
    d, _ = decide(make_interp("append", cand(ctx, "t5", 0.95, item="t5.i1")))
    assert d.kind == "CLARIFY" and d.questions[0].type == "content_required"


def test_question_order():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create",
                              cand(ctx, "t3", 0.88, fields={"t3.f1": val("x"), "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5),
                                                            "t3.f6": amb("t3.f6.o1", "t3.f6.o2")}),
                              cand(ctx, "t2", 0.85, fields={"t2.f1": val("x")})))
    assert [q.type for q in d.questions] == ["target", "field_required", "field_ambiguous", "date"]


def test_search_executes_without_item():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("search", cand(ctx, "t2", 0.95, search_query="Rimi")))
    assert d.kind == "EXECUTE"
```

- [ ] **Step 2:** run → ModuleNotFoundError.

- [ ] **Step 3: `app/validation/policy.py`**

```python
"""Deterministic decision layer: thresholds in, EXECUTE / CLARIFY / REJECT out."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from app.config import Settings
from app.validation.semantic import ValidationResult, VCandidate, VField

Kind = Literal["EXECUTE", "CLARIFY", "REJECT"]
Risk = Literal["LOW", "MEDIUM"]
QType = Literal["target", "item", "item_not_found", "field_required", "field_ambiguous",
                "field_confirm", "date", "content_required"]
RISK_BY_INTENT: dict[str, Risk] = {"create": "LOW", "append": "LOW", "search": "LOW",
                                   "update": "MEDIUM"}
_ORDER: dict[str, int] = {t: i for i, t in enumerate(
    ["target", "item", "item_not_found", "field_required", "field_ambiguous", "date",
     "field_confirm", "content_required"])}


@dataclass(frozen=True)
class Thresholds:
    intent_min: float
    target_min: float
    target_margin: float
    field_min: float
    date_min: float

    @classmethod
    def from_settings(cls, s: Settings) -> Thresholds:
        return cls(s.policy_intent_min, s.policy_target_min, s.policy_target_margin,
                   s.policy_field_min, s.policy_date_min)


@dataclass(frozen=True)
class QOption:
    key: str
    label: str


@dataclass
class Question:
    type: QType
    target_key: str | None = None
    field_key: str | None = None
    field_name: str | None = None
    options: list[QOption] = field(default_factory=list)
    proposed: Any = None


@dataclass
class Decision:
    kind: Kind
    candidate: VCandidate | None
    questions: list[Question]
    reasons: list[str]
    risk: Risk | None


def _label(v: Any) -> str:
    if hasattr(v, "name"):
        return str(v.name)
    if isinstance(v, list):
        return ", ".join(_label(x) for x in v)
    return str(v)


class Policy:
    def __init__(self, t: Thresholds) -> None:
        self.t = t

    def evaluate(self, r: ValidationResult) -> Decision:
        if r.rejected:
            reasons = [f"{i.code}: {i.message}" for i in r.issues] or ["no valid candidates"]
            return Decision("REJECT", None, [], reasons, None)
        best = r.best
        assert best is not None
        risk = RISK_BY_INTENT[r.intent]
        qs: list[Question] = []

        second = r.second
        if (r.intent_confidence < self.t.intent_min or best.confidence < self.t.target_min
                or (second is not None and best.confidence - second.confidence < self.t.target_margin)):
            qs.append(Question("target", best.key,
                               options=[QOption(c.key, c.target.name) for c in r.candidates]))

        if r.intent == "update" and best.item is None:
            if best.item_candidates:
                qs.append(Question("item", best.key,
                                   options=[QOption(self._item_key(best, i), i.title)
                                            for i in best.item_candidates]))
            else:
                q = Question("item_not_found", best.key)
                return Decision("REJECT", best, [q], ["item not found in target"], risk)
        if r.intent == "append" and best.item is None and best.item_candidates:
            qs.append(Question("item", best.key,
                               options=[QOption(self._item_key(best, i), i.title)
                                        for i in best.item_candidates]))

        for fk, f in best.fields.items():
            if r.intent == "create" and f.field.required and f.status in ("not_mentioned", "explicit_null"):
                qs.append(self._field_q("field_required", best, fk, f))
            elif f.status == "ambiguous":
                qs.append(self._field_q("field_ambiguous", best, fk, f))
            elif f.status == "value" and f.field.type == "date" and f.confidence < self.t.date_min:
                qs.append(Question("date", best.key, fk, f.field.name, proposed=f.value))
            elif f.status == "value" and f.field.type != "date" and f.confidence < self.t.field_min:
                qs.append(Question("field_confirm", best.key, fk, f.field.name, proposed=f.value))

        if r.intent == "append" and not best.content:
            qs.append(Question("content_required", best.key))

        if qs:
            qs.sort(key=lambda q: _ORDER[q.type])
            return Decision("CLARIFY", best, qs, [q.type for q in qs], risk)
        return Decision("EXECUTE", best, [], [], risk)

    @staticmethod
    def _item_key(c: VCandidate, item: Any) -> str:
        return f"{c.key}.item:{item.id}"

    @staticmethod
    def _field_q(qtype: QType, c: VCandidate, fk: str, f: VField) -> Question:
        if qtype == "field_ambiguous":
            options = [QOption(f"{fk}#{i}", _label(v)) for i, v in enumerate(f.candidates)]
        elif f.field.options:
            options = [QOption(f"{fk}.o{i + 1}", o.name) for i, o in enumerate(f.field.options)]
        else:
            options = []
        return Question(qtype, c.key, fk, f.field.name, options)
```

Note on option keys for `field_required`: they are the context option keys (`t3.f2.o1`), computed positionally from `f.field.options`, which is how `ContextBuilder` numbers them. For `item` questions the key is `<target>.item:<page id>` because the answer must survive a context rebuild; Plan 3 resolves it by page id.

- [ ] **Step 4:** tests pass (≈20 incl. parametrized). Ruff. Commit `feat: policy engine with configurable thresholds and structured questions`.

---

### Task 3: Command models, builder, Notion mapper

**Files:**
- Create: `app/commands/__init__.py`, `app/commands/models.py`, `app/commands/builder.py`, `app/notion/mapper.py`, `tests/test_commands.py`, `tests/test_mapper.py`

**Interfaces:**
- `models.py`: `PropertyWrite(property_id, property_name, type, value: Any)` — `value` JSON-able: str | int | float | bool | `{"start","end"}` | `{"id","name"}` | list of those | `None` (= clear); commands `CreateItem(action="create_item", data_source_id, target_name, properties: list[PropertyWrite])`, `UpdateItem(action="update_item", page_id, target_name, item_title, properties)`, `CreatePage(action="create_page", parent_page_id, target_name, title, body: list[str])`, `AppendBlocks(action="append_blocks", page_id, target_name, page_title, paragraphs: list[str])`, `Search(action="search", data_source_id: str | None, target_name, title_property: str | None, query)`; `Command = CreateItem | UpdateItem | CreatePage | AppendBlocks | Search`; `RISK: dict[str, str]`.
- `builder.py`: `build_command(c: VCandidate, intent: str, raw_text: str) -> Command`; `paragraphs(text: str | None) -> list[str]`; `to_json_value(v) -> Any`; `PAGE_TITLE_FIELD_ID` reuse.
- `mapper.py`: `property_payload(p: PropertyWrite) -> dict | None` (None when the write must be skipped); `properties_payload(props) -> dict`; `create_item_payload(cmd) -> tuple[dict, dict]`; `create_page_payload(cmd) -> tuple[dict, dict, list[dict]]`; `paragraph_blocks(paragraphs) -> list[dict]` (split each paragraph into ≤2000-char rich text chunks); `search_filter(cmd) -> dict | None`; `read_to_write(prop: dict) -> dict | None`; `RICH_TEXT_LIMIT = 2000`.

- [ ] **Step 1: failing tests** — `tests/test_commands.py`:

```python
from app.commands.builder import build_command, paragraphs
from app.commands.models import AppendBlocks, CreateItem, CreatePage, Search, UpdateItem
from app.validation.semantic import SemanticValidator
from tests.helpers import cand, ctx_and_snapshot, make_interp, val


def best(intent, c):
    ctx, snap = ctx_and_snapshot()
    return SemanticValidator().validate(make_interp(intent, c), ctx, snap).best


def test_paragraphs():
    assert paragraphs("a\n\n b \nc") == ["a", "b", "c"]
    assert paragraphs("   ") == [] and paragraphs(None) == [] and paragraphs("x") == ["x"]


def test_create_item_command():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t2", fields={
        "t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1"), "t2.f4": val(2), "t2.f5": val(True),
        "t2.f3": {"status": "explicit_null"}}))
    cmd = build_command(c, "create", "купи молоко")
    assert isinstance(cmd, CreateItem) and cmd.data_source_id == "ds-buy" and cmd.target_name == "Покупки"
    by_name = {p.property_name: p for p in cmd.properties}
    assert by_name["Название"].value == "Молоко" and by_name["Название"].property_id == "title"
    assert by_name["Магазин"].value == {"id": "o-Rimi", "name": "Rimi"}
    assert by_name["Количество"].value == 2 and by_name["Куплено"].value is True
    assert by_name["Категория"].value is None                     # explicit_null → clear
    assert "Заметка" not in by_name                                # not_mentioned → omitted
    assert cmd.model_dump()["action"] == "create_item"


def test_update_item_command_and_date_relation_json():
    ctx, _ = ctx_and_snapshot()
    c = best("update", cand(ctx, "t3", item="t3.i1", fields={
        "t3.f3": val({"start": "2026-09-11T10:00", "end": None}), "t3.f5": val(["t3.f5.o1"]),
        "t3.f4": val("t3.f4.o3")}))
    cmd = build_command(c, "update", "")
    assert isinstance(cmd, UpdateItem) and cmd.page_id == "t-docs" and cmd.item_title == "Подготовить документы"
    by_name = {p.property_name: p for p in cmd.properties}
    assert by_name["Срок"].value == {"start": "2026-09-11T10:00:00+03:00", "end": None}
    assert by_name["Проект"].value == [{"id": "p-home", "name": "Дом"}]
    assert by_name["Статус"].value == {"id": "o-Done", "name": "Done"}
    cmd.model_dump_json()  # serialisable for the audit log


def test_create_page_and_append_and_search():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t5", fields={"t5.f1": val("Отпуск 2028")}, content="план\nбюджет"))
    cmd = build_command(c, "create", "")
    assert isinstance(cmd, CreatePage) and cmd.parent_page_id == "pg-ideas" and cmd.title == "Отпуск 2028"
    assert cmd.body == ["план", "бюджет"]
    c = best("append", cand(ctx, "t5", item="t5.i1", content="взять палатку"))
    cmd = build_command(c, "append", "")
    assert isinstance(cmd, AppendBlocks) and cmd.page_id == "pg-trip" and cmd.page_title == "Отпуск 2027"
    c = best("append", cand(ctx, "t5", content="идея"))
    assert build_command(c, "append", "").page_id == "pg-ideas"
    c = best("search", cand(ctx, "t2", search_query="Rimi"))
    cmd = build_command(c, "search", "что в покупках на Rimi")
    assert isinstance(cmd, Search) and cmd.data_source_id == "ds-buy" and cmd.title_property == "Название"
    assert cmd.query == "Rimi"
    c = best("search", cand(ctx, "t2"))
    assert build_command(c, "search", "что в покупках").query == "что в покупках"
```

`tests/test_mapper.py`:

```python
from app.commands.models import AppendBlocks, CreateItem, CreatePage, PropertyWrite, Search
from app.notion.mapper import (
    RICH_TEXT_LIMIT,
    create_item_payload,
    create_page_payload,
    paragraph_blocks,
    properties_payload,
    property_payload,
    read_to_write,
    search_filter,
)


def pw(pid, name, type, value):
    return PropertyWrite(property_id=pid, property_name=name, type=type, value=value)


def test_property_payloads():
    assert property_payload(pw("t", "Название", "title", "Молоко")) == {
        "title": [{"type": "text", "text": {"content": "Молоко"}}]}
    assert property_payload(pw("n", "Заметка", "rich_text", "x")) == {
        "rich_text": [{"type": "text", "text": {"content": "x"}}]}
    assert property_payload(pw("n", "Заметка", "rich_text", None)) == {"rich_text": []}
    assert property_payload(pw("s", "Магазин", "select", {"id": "o1", "name": "Rimi"})) == {"select": {"id": "o1"}}
    assert property_payload(pw("s", "Магазин", "select", None)) == {"select": None}
    assert property_payload(pw("s", "Статус", "status", {"id": "o3", "name": "Done"})) == {"status": {"id": "o3"}}
    assert property_payload(pw("s", "Статус", "status", None)) is None
    assert property_payload(pw("m", "Теги", "multi_select", [{"id": "a", "name": "x"}])) == {"multi_select": [{"id": "a"}]}
    assert property_payload(pw("m", "Теги", "multi_select", None)) == {"multi_select": []}
    assert property_payload(pw("r", "Проект", "relation", [{"id": "p1", "name": "Дом"}])) == {"relation": [{"id": "p1"}]}
    assert property_payload(pw("d", "Срок", "date", {"start": "2026-09-11", "end": None})) == {"date": {"start": "2026-09-11", "end": None}}
    assert property_payload(pw("d", "Срок", "date", None)) == {"date": None}
    assert property_payload(pw("c", "Куплено", "checkbox", True)) == {"checkbox": True}
    assert property_payload(pw("c", "Куплено", "checkbox", None)) == {"checkbox": False}
    assert property_payload(pw("q", "Количество", "number", 2)) == {"number": 2}
    assert property_payload(pw("u", "Ссылка", "url", "https://x")) == {"url": "https://x"}
    assert property_payload(pw("u", "Ссылка", "url", None)) == {"url": None}


def test_properties_payload_keys_by_id_and_skips_none():
    props = [pw("s", "Статус", "status", None), pw("t", "Название", "title", "x")]
    assert list(properties_payload(props)) == ["t"]


def test_create_item_and_page_payloads():
    cmd = CreateItem(data_source_id="ds", target_name="Покупки", properties=[pw("t", "Название", "title", "x")])
    parent, props = create_item_payload(cmd)
    assert parent == {"type": "data_source_id", "data_source_id": "ds"} and "t" in props
    page = CreatePage(parent_page_id="pg", target_name="Идеи", title="Отпуск", body=["a", "b"])
    parent, props, children = create_page_payload(page)
    assert parent == {"type": "page_id", "page_id": "pg"}
    assert props == {"title": {"title": [{"type": "text", "text": {"content": "Отпуск"}}]}}
    assert [c["paragraph"]["rich_text"][0]["text"]["content"] for c in children] == ["a", "b"]


def test_paragraph_blocks_split_long_text():
    blocks = paragraph_blocks(["x" * (RICH_TEXT_LIMIT + 5)])
    assert len(blocks) == 1 and len(blocks[0]["paragraph"]["rich_text"]) == 2
    assert blocks[0]["object"] == "block" and blocks[0]["type"] == "paragraph"
    assert paragraph_blocks([]) == []


def test_search_filter():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название", query="Rimi")
    assert search_filter(cmd) == {"property": "Название", "title": {"contains": "Rimi"}}
    assert search_filter(Search(data_source_id=None, target_name="Идеи", title_property=None, query="x")) is None


def test_read_to_write_roundtrip_shapes():
    assert read_to_write({"type": "title", "title": [{"plain_text": "Хлеб"}]}) == {
        "title": [{"type": "text", "text": {"content": "Хлеб"}}]}
    assert read_to_write({"type": "select", "select": {"id": "o1", "name": "Rimi"}}) == {"select": {"id": "o1"}}
    assert read_to_write({"type": "select", "select": None}) == {"select": None}
    assert read_to_write({"type": "status", "status": {"id": "o2"}}) == {"status": {"id": "o2"}}
    assert read_to_write({"type": "multi_select", "multi_select": [{"id": "a"}, {"id": "b"}]}) == {"multi_select": [{"id": "a"}, {"id": "b"}]}
    assert read_to_write({"type": "relation", "relation": [{"id": "p1"}]}) == {"relation": [{"id": "p1"}]}
    assert read_to_write({"type": "date", "date": {"start": "2026-09-11", "end": None}}) == {"date": {"start": "2026-09-11", "end": None}}
    assert read_to_write({"type": "checkbox", "checkbox": False}) == {"checkbox": False}
    assert read_to_write({"type": "number", "number": None}) == {"number": None}
    assert read_to_write({"type": "url", "url": "https://x"}) == {"url": "https://x"}
    assert read_to_write({"type": "formula", "formula": {}}) is None
    assert read_to_write({"type": "rich_text", "rich_text": []}) == {"rich_text": []}
```

- [ ] **Step 2:** run → ModuleNotFoundError.

- [ ] **Step 3: `app/commands/models.py`**

```python
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyWrite(Strict):
    property_id: str
    property_name: str
    type: str
    value: Any = None  # None = clear


class CreateItem(Strict):
    action: Literal["create_item"] = "create_item"
    data_source_id: str
    target_name: str
    properties: list[PropertyWrite]


class UpdateItem(Strict):
    action: Literal["update_item"] = "update_item"
    page_id: str
    target_name: str
    item_title: str
    properties: list[PropertyWrite]


class CreatePage(Strict):
    action: Literal["create_page"] = "create_page"
    parent_page_id: str
    target_name: str
    title: str
    body: list[str] = []


class AppendBlocks(Strict):
    action: Literal["append_blocks"] = "append_blocks"
    page_id: str
    target_name: str
    page_title: str
    paragraphs: list[str]


class Search(Strict):
    action: Literal["search"] = "search"
    data_source_id: str | None
    target_name: str
    title_property: str | None
    query: str


Command = CreateItem | UpdateItem | CreatePage | AppendBlocks | Search
RISK: dict[str, str] = {"create_item": "LOW", "create_page": "LOW", "append_blocks": "LOW",
                        "search": "LOW", "update_item": "MEDIUM"}
```

- [ ] **Step 4: `app/commands/builder.py`**

```python
"""VCandidate → Command. The only place that decides which fields get written."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.commands.models import AppendBlocks, Command, CreateItem, CreatePage, PropertyWrite, Search, UpdateItem
from app.llm.context import PAGE_TITLE_FIELD_ID
from app.notion.snapshot import Option
from app.validation.semantic import DateRange, VCandidate, VField


def paragraphs(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


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


def _writes(c: VCandidate, *, include_title: bool = True) -> list[PropertyWrite]:
    out: list[PropertyWrite] = []
    for f in c.fields.values():
        if f.field.id == PAGE_TITLE_FIELD_ID or f.status not in ("value", "explicit_null"):
            continue
        if f.field.type == "title" and not include_title:
            continue
        out.append(PropertyWrite(property_id=f.field.id, property_name=f.field.name, type=f.field.type,
                                 value=to_json_value(f.value) if f.status == "value" else None))
    return out


def _title_value(c: VCandidate) -> str | None:
    f: VField | None = next((f for f in c.fields.values() if f.field.type == "title"), None)
    return f.value if f and f.status == "value" else None


def build_command(c: VCandidate, intent: str, raw_text: str) -> Command:
    t = c.target
    if intent == "create" and t.kind == "database":
        return CreateItem(data_source_id=t.id, target_name=t.name, properties=_writes(c))
    if intent == "create":
        return CreatePage(parent_page_id=t.id, target_name=t.name, title=_title_value(c) or raw_text.strip(),
                          body=paragraphs(c.content))
    if intent == "update":
        assert c.item is not None
        return UpdateItem(page_id=c.item.id, target_name=t.name, item_title=c.item.title, properties=_writes(c))
    if intent == "append":
        page = c.item
        return AppendBlocks(page_id=page.id if page else t.id, target_name=t.name,
                            page_title=page.title if page else t.name, paragraphs=paragraphs(c.content))
    if intent == "search":
        title = t.title_field()
        return Search(data_source_id=t.id if t.kind == "database" else None, target_name=t.name,
                      title_property=title.name if title else None,
                      query=c.search_query or _title_value(c) or raw_text.strip())
    raise ValueError(f"unsupported intent {intent}")
```

- [ ] **Step 5: `app/notion/mapper.py`**

```python
"""Command → Notion API JSON. Nothing else in the app builds Notion payloads."""

from __future__ import annotations

from typing import Any

from app.commands.models import CreateItem, CreatePage, PropertyWrite, Search

RICH_TEXT_LIMIT = 2000


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[i:i + RICH_TEXT_LIMIT]}}
            for i in range(0, len(text), RICH_TEXT_LIMIT)] if text else []


def property_payload(p: PropertyWrite) -> dict | None:
    v: Any = p.value
    t = p.type
    if t in ("title", "rich_text"):
        return {t: _rich(v or "")}
    if t == "select":
        return {"select": {"id": v["id"]} if v else None}
    if t == "status":
        return {"status": {"id": v["id"]}} if v else None
    if t in ("multi_select", "relation"):
        return {t: [{"id": x["id"]} for x in (v or [])]}
    if t == "date":
        return {"date": {"start": v["start"], "end": v.get("end")} if v else None}
    if t == "checkbox":
        return {"checkbox": bool(v)}
    if t == "number":
        return {"number": v}
    if t == "url":
        return {"url": v}
    return None


def properties_payload(props: list[PropertyWrite]) -> dict:
    out: dict = {}
    for p in props:
        payload = property_payload(p)
        if payload is not None:
            out[p.property_id] = payload
    return out


def paragraph_blocks(paragraphs: list[str]) -> list[dict]:
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(p)}}
            for p in paragraphs if p]


def create_item_payload(cmd: CreateItem) -> tuple[dict, dict]:
    return {"type": "data_source_id", "data_source_id": cmd.data_source_id}, properties_payload(cmd.properties)


def create_page_payload(cmd: CreatePage) -> tuple[dict, dict, list[dict]]:
    return ({"type": "page_id", "page_id": cmd.parent_page_id},
            {"title": {"title": _rich(cmd.title)}},
            paragraph_blocks(cmd.body))


def search_filter(cmd: Search) -> dict | None:
    if not cmd.data_source_id or not cmd.title_property:
        return None
    return {"property": cmd.title_property, "title": {"contains": cmd.query}}


def read_to_write(prop: dict) -> dict | None:
    """Turn a property from a page *read* into the payload that restores it (for undo)."""
    t = prop.get("type")
    v = prop.get(t) if t else None
    if t in ("title", "rich_text"):
        return {t: _rich("".join(r.get("plain_text", "") for r in (v or [])))}
    if t in ("select", "status"):
        return {t: {"id": v["id"]} if v else None}
    if t in ("multi_select", "relation"):
        return {t: [{"id": x["id"]} for x in (v or [])]}
    if t == "date":
        return {"date": {"start": v["start"], "end": v.get("end")} if v else None}
    if t in ("checkbox", "number", "url"):
        return {t: v}
    return None
```

- [ ] **Step 6:** tests pass. Ruff (wrap long import line). Commit `feat: command models, builder and Notion payload mapper`.

---

### Task 4: Executor with undo

**Files:**
- Create: `app/commands/executor.py`, `tests/test_executor.py`
- Modify: `tests/fakes.py` — add `self.pages: dict[str, dict] = {}` in `__init__`; `get_page` returns `self.pages.get(page_id, {"id": page_id, "properties": {}})`; `create_page` returns `{"id": "new-page", "url": "https://notion.so/new-page", "properties": properties}` (unchanged); `update_page` unchanged.

**Interfaces:**
- `Written(name: str, value: Any)` — what the user should see (JSON-able value from `PropertyWrite`); `SearchHit(title: str, url: str, page_id: str)`; `UndoRecord(BaseModel)` with `kind: "archive" | "restore" | "delete_blocks"`, `page_id: str | None = None`, `properties: dict | None = None`, `block_ids: list[str] = []`; `ExecutionResult(command, page_id, url, block_ids, written: list[Written], undo: UndoRecord | None, hits: list[SearchHit])`; `class Executor(provider: NotionProvider)` with `async run(cmd) -> ExecutionResult`, `async undo(rec: UndoRecord) -> None`; `SEARCH_LIMIT = 20`.

- [ ] **Step 1: failing tests** — `tests/test_executor.py`:

```python
import pytest

from app.commands.executor import Executor, UndoRecord
from app.commands.models import AppendBlocks, CreateItem, CreatePage, PropertyWrite, Search, UpdateItem
from app.notion.errors import NotionError
from tests.fakes import FakeNotionProvider


def pw(pid, name, type, value):
    return PropertyWrite(property_id=pid, property_name=name, type=type, value=value)


@pytest.fixture
def fake():
    return FakeNotionProvider()


async def test_create_item(fake):
    ex = Executor(fake)
    r = await ex.run(CreateItem(data_source_id="ds", target_name="Покупки",
                                properties=[pw("title", "Название", "title", "Молоко"),
                                            pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"})]))
    assert r.page_id == "new-page" and r.url == "https://notion.so/new-page"
    assert [(w.name, w.value) for w in r.written] == [("Название", "Молоко"), ("Магазин", {"id": "o1", "name": "Rimi"})]
    assert r.undo == UndoRecord(kind="archive", page_id="new-page")
    call = fake.calls[-1]
    assert call[0] == "create_page" and call[1] == {"type": "data_source_id", "data_source_id": "ds"}
    assert call[2]["shop"] == {"select": {"id": "o1"}}


async def test_update_item_records_previous_values(fake):
    fake.pages["p1"] = {"id": "p1", "url": "https://notion.so/p1", "properties": {
        "Куплено": {"id": "done", "type": "checkbox", "checkbox": False},
        "Магазин": {"id": "shop", "type": "select", "select": {"id": "o2", "name": "Prisma"}},
        "Название": {"id": "title", "type": "title", "title": [{"plain_text": "Молоко"}]},
    }}
    ex = Executor(fake)
    r = await ex.run(UpdateItem(page_id="p1", target_name="Покупки", item_title="Молоко",
                                properties=[pw("done", "Куплено", "checkbox", True), pw("shop", "Магазин", "select", None)]))
    assert r.page_id == "p1" and r.url == "https://notion.so/p1"
    assert r.undo.kind == "restore" and r.undo.page_id == "p1"
    assert r.undo.properties == {"done": {"checkbox": False}, "shop": {"select": {"id": "o2"}}}
    assert fake.calls[-1] == ("update_page", "p1", {"done": {"checkbox": True}, "shop": {"select": None}}, None)


async def test_create_page_and_append(fake):
    ex = Executor(fake)
    r = await ex.run(CreatePage(parent_page_id="pg", target_name="Идеи", title="Отпуск", body=["a"]))
    assert r.undo.kind == "archive" and fake.calls[-1][1] == {"type": "page_id", "page_id": "pg"}
    assert fake.calls[-1][3][0]["paragraph"]["rich_text"][0]["text"]["content"] == "a"
    r = await ex.run(AppendBlocks(page_id="pg", target_name="Идеи", page_title="Идеи", paragraphs=["x", "y"]))
    assert r.block_ids == ["blk-0", "blk-1"] and r.undo == UndoRecord(kind="delete_blocks", block_ids=["blk-0", "blk-1"])
    assert r.page_id == "pg"


async def test_search_in_data_source_and_workspace(fake):
    fake.data_sources["ds"] = {"id": "ds"}
    fake.items["ds"] = [{"id": f"r{i}", "url": f"https://notion.so/r{i}",
                         "properties": {"N": {"type": "title", "title": [{"plain_text": f"Товар {i}"}]}}}
                        for i in range(25)]
    ex = Executor(fake)
    r = await ex.run(Search(data_source_id="ds", target_name="Покупки", title_property="N", query="Товар"))
    assert len(r.hits) == 20 and r.hits[0].title == "Товар 0" and r.hits[0].page_id == "r0"
    assert r.undo is None
    fake.search_results = [{"object": "page", "id": "pg", "url": "https://notion.so/pg",
                            "properties": {"title": {"type": "title", "title": [{"plain_text": "Идеи"}]}}},
                           {"object": "data_source", "id": "ds"}]
    r = await ex.run(Search(data_source_id=None, target_name="Идеи", title_property=None, query="Идеи"))
    assert [h.title for h in r.hits] == ["Идеи"] and fake.calls[-1] == ("search", "Идеи", "page")


async def test_undo_kinds(fake):
    ex = Executor(fake)
    await ex.undo(UndoRecord(kind="archive", page_id="p"))
    assert fake.calls[-1] == ("update_page", "p", None, True)
    await ex.undo(UndoRecord(kind="restore", page_id="p", properties={"done": {"checkbox": False}}))
    assert fake.calls[-1] == ("update_page", "p", {"done": {"checkbox": False}}, None)
    await ex.undo(UndoRecord(kind="delete_blocks", block_ids=["a", "b"]))
    assert fake.calls[-2:] == [("delete_block", "a"), ("delete_block", "b")]


async def test_notion_errors_propagate(fake):
    async def boom(*a, **k):
        raise NotionError(400, "validation_error", "bad")

    fake.create_page = boom
    with pytest.raises(NotionError):
        await Executor(fake).run(CreateItem(data_source_id="ds", target_name="x", properties=[]))
```

- [ ] **Step 2:** run → ModuleNotFoundError.

- [ ] **Step 3: `app/commands/executor.py`**

```python
"""Runs commands through the NotionProvider and produces undo records."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.commands.models import AppendBlocks, Command, CreateItem, CreatePage, Search, UpdateItem
from app.notion import props
from app.notion.mapper import (
    create_item_payload,
    create_page_payload,
    paragraph_blocks,
    properties_payload,
    read_to_write,
    search_filter,
)
from app.notion.provider import NotionProvider

SEARCH_LIMIT = 20


@dataclass(frozen=True)
class Written:
    name: str
    value: Any


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    page_id: str


class UndoRecord(BaseModel):
    kind: Literal["archive", "restore", "delete_blocks"]
    page_id: str | None = None
    properties: dict | None = None
    block_ids: list[str] = []


@dataclass
class ExecutionResult:
    command: Command
    page_id: str | None = None
    url: str | None = None
    block_ids: list[str] = field(default_factory=list)
    written: list[Written] = field(default_factory=list)
    undo: UndoRecord | None = None
    hits: list[SearchHit] = field(default_factory=list)


class Executor:
    def __init__(self, provider: NotionProvider) -> None:
        self._p = provider

    async def run(self, cmd: Command) -> ExecutionResult:
        if isinstance(cmd, CreateItem):
            parent, properties = create_item_payload(cmd)
            page = await self._p.create_page(parent, properties)
            return ExecutionResult(cmd, page.get("id"), page.get("url"),
                                   written=[Written(p.property_name, p.value) for p in cmd.properties],
                                   undo=UndoRecord(kind="archive", page_id=page.get("id")))
        if isinstance(cmd, UpdateItem):
            before = await self._p.get_page(cmd.page_id)
            wanted = {p.property_id for p in cmd.properties}
            previous: dict = {}
            for prop in before.get("properties", {}).values():
                if prop.get("id") in wanted:
                    restore = read_to_write(prop)
                    if restore is not None:
                        previous[prop["id"]] = restore
            page = await self._p.update_page(cmd.page_id, properties=properties_payload(cmd.properties))
            return ExecutionResult(cmd, cmd.page_id, page.get("url") or before.get("url"),
                                   written=[Written(p.property_name, p.value) for p in cmd.properties],
                                   undo=UndoRecord(kind="restore", page_id=cmd.page_id, properties=previous))
        if isinstance(cmd, CreatePage):
            parent, properties, children = create_page_payload(cmd)
            page = await self._p.create_page(parent, properties, children or None)
            return ExecutionResult(cmd, page.get("id"), page.get("url"),
                                   written=[Written("title", cmd.title)],
                                   undo=UndoRecord(kind="archive", page_id=page.get("id")))
        if isinstance(cmd, AppendBlocks):
            data = await self._p.append_blocks(cmd.page_id, paragraph_blocks(cmd.paragraphs))
            ids = [b["id"] for b in data.get("results", []) if "id" in b]
            return ExecutionResult(cmd, cmd.page_id, None, block_ids=ids,
                                   written=[Written("paragraphs", cmd.paragraphs)],
                                   undo=UndoRecord(kind="delete_blocks", block_ids=ids))
        if isinstance(cmd, Search):
            return ExecutionResult(cmd, hits=await self._search(cmd))
        raise TypeError(f"unsupported command {type(cmd).__name__}")

    async def _search(self, cmd: Search) -> list[SearchHit]:
        if cmd.data_source_id:
            rows = await self._p.query_data_source(cmd.data_source_id, filter=search_filter(cmd),
                                                   page_size=SEARCH_LIMIT)
        else:
            rows = [r for r in await self._p.search(cmd.query, "page") if r.get("object") == "page"]
        return [SearchHit(props.page_title(r), r.get("url", ""), r["id"]) for r in rows[:SEARCH_LIMIT]]

    async def undo(self, rec: UndoRecord) -> None:
        if rec.kind == "archive" and rec.page_id:
            await self._p.update_page(rec.page_id, archived=True)
        elif rec.kind == "restore" and rec.page_id:
            await self._p.update_page(rec.page_id, properties=rec.properties or {})
        elif rec.kind == "delete_blocks":
            for bid in rec.block_ids:
                await self._p.delete_block(bid)
```

- [ ] **Step 4:** tests pass. Ruff. Commit `feat: executor with undo records` (include `tests/fakes.py`).

---

### Task 5: Docs sync

**Files:**
- Modify: `documentation/DATA_MODEL.md` §4–§5 (question keys: `<target>.item:<page id>`, `<field>#<index>`, option keys; `PropertyWrite` shape; `UndoRecord`), `documentation/ARCHITECTURE.md` §8 (item_not_found REJECT carries the candidate so Plan 3 can offer "create instead"; `explicit_null` on status dropped; risk recorded), `documentation/ERRORS.md` (add `SEM_STATUS_CLEAR`, `INTENT_UNKNOWN`).

- [ ] **Step 1:** make the three edits; keep them short and exact to the code.
- [ ] **Step 2:** `uv run pytest -q`, ruff. Commit `docs: data model and errors for validation/commands layer`.

---

## Self-review

- Spec coverage: T-030 (Task 1), T-031 (Task 2), T-032 (Task 3 models+builder), T-033 (Task 3 mapper), T-034 (Task 4). Plan 2a rulings: dedupe by target (`test_dedupe_and_sort_candidates`), typing per field (`test_type_errors_invalidate_candidate`), caps (`test_items_and_candidates_resolved_and_capped`, `test_page_title_field_and_text_caps`).
- Type consistency: `VCandidate.fields: dict[str, VField]` with `VField.field: Field` used by policy and builder; `PropertyWrite.value` JSON shapes consumed by `property_payload`; `read_to_write` output shapes match `UndoRecord.properties` and `update_page(properties=...)`; `Question.options: list[QOption]`.
- Known simplifications: rich text formatting is flattened on restore; `status` cannot be cleared; search matches title `contains` only; the `field_confirm` question is asked for non-date low-confidence values (Plan 3 may choose to auto-accept for optional fields).
