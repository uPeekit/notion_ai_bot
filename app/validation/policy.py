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
                or (second is not None
                    and round(best.confidence - second.confidence, 9) < self.t.target_margin)):
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
            if (r.intent == "create" and f.field.required
                    and f.status in ("not_mentioned", "explicit_null")):
                qs.append(self._field_q("field_required", best, fk, f))
            elif f.status == "ambiguous":
                qs.append(self._field_q("field_ambiguous", best, fk, f))
            elif f.status == "value" and f.field.type == "date" and f.confidence < self.t.date_min:
                qs.append(Question("date", best.key, fk, f.field.name, proposed=f.value))
            elif (f.status == "value" and f.field.type != "date"
                    and f.confidence < self.t.field_min):
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
