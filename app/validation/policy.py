"""Deterministic decision layer: thresholds in, EXECUTE / CLARIFY / REJECT out."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.commands.jsonvalue import to_json_value
from app.config import Settings
from app.validation.semantic import DateRange, ValidationResult, VCandidate, VField

Kind = Literal["EXECUTE", "CLARIFY", "REJECT"]
Risk = Literal["LOW", "MEDIUM"]
QType = Literal["target", "intent_confirm", "item", "item_not_found", "field_required",
                "field_ambiguous", "field_confirm", "date", "content_required",
                "nothing_to_write", "clarify"]
RISK_BY_INTENT: dict[str, Risk] = {"create": "LOW", "append": "LOW", "search": "LOW",
                                   "update": "MEDIUM"}
_ORDER: dict[str, int] = {t: i for i, t in enumerate(
    ["clarify", "target", "intent_confirm", "item", "item_not_found", "field_required",
     "field_ambiguous", "date", "field_confirm", "content_required", "nothing_to_write"])}


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


class QOption(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    label: str


class Question(BaseModel):
    """A JSON-safe clarification question; a session can persist these across a restart or a
    context rebuild (Plan 3)."""

    model_config = ConfigDict(extra="forbid")
    type: QType
    target_key: str | None = None
    field_key: str | None = None
    field_name: str | None = None
    options: list[QOption] = Field(default_factory=list)
    proposed: Any = None

    @property
    def id(self) -> str:
        """Stable within one context generation (identifies *what* is being asked, not which
        request). It embeds positional context keys, so it does NOT survive a rediscovery —
        after a context rebuild, re-resolve the answer through Context.field_key/option_key/
        item_key instead of comparing ids across generations."""
        return f"{self.type}:{self.field_key or self.target_key or ''}"


def _proposed_json(v: Any) -> Any:
    """JSON-safe form of a typed field value for Question.proposed."""
    if isinstance(v, DateRange):
        granularity = "datetime" if isinstance(v.start, datetime) else "date"
        return {
            "start": v.start.isoformat(),
            "end": v.end.isoformat() if v.end else None,
            "granularity": granularity,
        }
    return to_json_value(v)


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
            if r.clarify:
                # The model could not settle on an action but wrote a question about it ("is
                # this a task or just a note?"). Asking that is worth more to the user than
                # "I did not understand" plus a copy of their message in the inbox, which is
                # what a rejection comes to. The answer is re-read together with the original
                # message, so a usable candidate (there may be none at all) is not needed.
                q = Question(type="clarify", target_key=r.best.key if r.best else None,
                             proposed=r.clarify)
                return Decision("CLARIFY", r.best, [q], ["clarify"], None)
            reasons = [f"{i.code}: {i.message}" for i in r.issues] or ["no valid candidates"]
            return Decision("REJECT", None, [], reasons, None)
        best = r.best
        assert best is not None
        risk = RISK_BY_INTENT[r.intent]
        if r.clarify:
            # The model itself says the message cannot be acted on as it stands: its question
            # goes first and alone; the free-text answer is re-read together with the message.
            q = Question(type="clarify", target_key=best.key, proposed=r.clarify)
            return Decision("CLARIFY", best, [q], ["clarify"], risk)
        qs: list[Question] = []

        second = r.second
        low_intent = r.intent_confidence < self.t.intent_min
        low_target = best.confidence < self.t.target_min
        low_margin = (second is not None
                      and round(best.confidence - second.confidence, 9) < self.t.target_margin)
        if low_intent or low_target or low_margin:
            if low_intent and not low_target and not low_margin and len(r.candidates) == 1:
                qs.append(Question(type="intent_confirm", target_key=best.key,
                                    proposed=r.intent))
            else:
                # `proposed` carries the intent so the question can say how the message was
                # read — a misread intent is otherwise invisible behind a list of places.
                qs.append(Question(
                    type="target", target_key=best.key,
                    options=[QOption(key=c.key, label=c.target.name) for c in r.candidates],
                    proposed=r.intent,
                ))

        if r.intent == "update" and best.item is None:
            if best.item_candidates:
                qs.append(Question(
                    type="item", target_key=best.key,
                    options=[QOption(key=self._item_key(best, i), label=i.title)
                             for i in best.item_candidates],
                ))
            else:
                q = Question(type="item_not_found", target_key=best.key,
                              proposed=best.item_text)
                return Decision("REJECT", best, [q], ["item not found in target"], risk)
        elif r.intent == "update":
            if not any(f.status in ("value", "explicit_null", "ambiguous")
                       for f in best.fields.values()):
                q = Question(type="nothing_to_write", target_key=best.key)
                return Decision("REJECT", best, [q], ["nothing to write"], risk)
        if r.intent == "append" and best.item is None and best.item_candidates:
            qs.append(Question(
                type="item", target_key=best.key,
                options=[QOption(key=self._item_key(best, i), label=i.title)
                         for i in best.item_candidates],
            ))

        for fk, f in best.fields.items():
            # A checkbox is never asked about: Notion has no empty one, so unticked is its
            # value and the question can only ask the user to confirm the default.
            if (r.intent == "create" and f.field.required and f.field.type != "checkbox"
                    and f.status in ("not_mentioned", "explicit_null")):
                qs.append(self._field_q("field_required", best, fk, f))
            elif f.status == "ambiguous":
                qs.append(self._field_q("field_ambiguous", best, fk, f))
            elif f.status == "value" and f.field.type == "date" and f.confidence < self.t.date_min:
                qs.append(Question(type="date", target_key=best.key, field_key=fk,
                                    field_name=f.field.name, proposed=_proposed_json(f.value)))
            elif (f.status == "value" and f.field.type != "date"
                    and f.confidence < self.t.field_min):
                qs.append(Question(type="field_confirm", target_key=best.key, field_key=fk,
                                    field_name=f.field.name, proposed=_proposed_json(f.value)))

        # A web query brings its own content: the research result is what gets appended.
        if r.intent == "append" and not best.content and not best.web_query:
            qs.append(Question(type="content_required", target_key=best.key))

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
            options = [QOption(key=f"{fk}#{i}", label=_label(v))
                       for i, v in enumerate(f.candidates)]
        elif f.field.options:
            options = [QOption(key=f"{fk}.o{i + 1}", label=o.name)
                       for i, o in enumerate(f.field.options)]
        else:
            options = []
        return Question(type=qtype, target_key=c.key, field_key=fk, field_name=f.field.name,
                         options=options)
