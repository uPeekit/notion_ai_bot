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
            issues.append(
                Issue("SEM_UNKNOWN_KEY", f"target {cand.target} not in snapshot", cand.target)
            )
            return None
        if not (INTENT_OPS[intent] & target.operations):
            issues.append(
                Issue(
                    "SEM_UNSUPPORTED_OP",
                    f"{intent} not supported for {target.name}",
                    cand.target,
                )
            )
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
                issues.append(
                    Issue("SEM_UNKNOWN_KEY", f"item {cand.item} not in {cand.target}", cand.item)
                )
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
        return VCandidate(
            cand.target, target, cand.confidence, item, item_candidates, fields,
            content or None, query or None,
        )

    @staticmethod
    def _field_def(target: Target, ref: KeyRef) -> Field:
        fdef = target.field(ref.field_id or "")
        if fdef is None:  # synthetic page title
            fdef = Field(
                id=ref.field_id or "", name=ref.name, type="title", required=True,
                options=[], relation_data_source_id=None, description="",
            )
        return fdef

    @staticmethod
    def _item(key: str, target_key: str, target: Target, ctx: Context) -> Item | None:
        if key not in ctx.item_keys(target_key):
            return None
        ref = ctx.ref(key)
        return target.item(ref.item_id) if ref and ref.item_id else None

    @staticmethod
    def _field(
        fk: str, ref: KeyRef, fdef: Field, raw: Any, ctx: Context, issues: list[Issue]
    ) -> VField:
        ftype = ref.field_type or fdef.type
        if raw is None or getattr(raw, "status", None) == "not_mentioned":
            return VField(fk, ref, fdef, "not_mentioned")
        if isinstance(raw, ExplicitNull):
            if ftype == "status":
                issues.append(
                    Issue("SEM_STATUS_CLEAR", f"{ref.name}: status cannot be cleared", fk)
                )
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
