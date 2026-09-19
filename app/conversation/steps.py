"""A structured plan step → the same answer shape the LLM produces, without asking it.

The planner already decided what a step means (a row in Books titled "Omon Ra", Status Read),
so re-reading that sentence with the model would cost a call per step — 20 books meant 20 calls
and minutes of waiting. Here the names are resolved against the request's own context instead:
target and field by name, option by option name, values by field type. The result is an
`Interpretation`, so everything downstream — validator, policy, executor, undo — is unchanged.

Anything that does not resolve (a tag the workspace does not have, an unknown target) returns
None, and the caller falls back to reading the step's `text` with the LLM."""

from __future__ import annotations

import logging
from typing import Any

from app import texts
from app.conversation.plan import StepSpec
from app.interpretation.models import Interpretation
from app.llm.context import Context, KeyRef

log = logging.getLogger(__name__)

LIST_TYPES = frozenset({"multi_select", "relation"})
OPTION_TYPES = LIST_TYPES | {"select", "status"}


def _norm(text: str) -> str:
    for quote in ("«", "»", '"', "“", "”"):  # «», ""
        text = text.replace(quote, "")
    return " ".join(text.split()).casefold()


def _find(ctx: Context, kind: str, name: str, *, prefix: str = "") -> str | None:
    """The context key of the target/field/option called `name` (case-insensitive), within
    `prefix` (a target key for fields, a field key for options)."""
    wanted = _norm(name)
    for key, ref in ctx.keys.items():
        if ref.kind != kind or (prefix and not key.startswith(prefix + ".")):
            continue
        if _norm(ref.name) == wanted:
            return key
    return None


def _target_key(ctx: Context, name: str) -> str | None:
    key = _find(ctx, "target", name)
    if key is not None:
        return key
    # The planner may name a target by its path ("Home / Shopping") or with its kind appended.
    wanted = _norm(name)
    for key, label in ctx.target_labels.items():
        if _norm(label) == wanted or wanted.endswith(_norm(ctx.keys[key].name)):
            return key
    return None


def _value(ctx: Context, field_key: str, ref: KeyRef, raw: str) -> Any:
    """The raw string as the field's type needs it, or None when it does not fit."""
    text = raw.strip()
    if not text:
        return None
    ftype = ref.field_type
    if ftype in OPTION_TYPES:
        names = [p for p in (text.split(",") if ftype in LIST_TYPES else [text]) if p.strip()]
        keys = [_find(ctx, "option", n, prefix=field_key) for n in names]
        if any(k is None for k in keys):
            return None
        return keys if ftype in LIST_TYPES else keys[0]
    if ftype == "checkbox":
        if text.casefold() in texts.BOOL_WORDS_TRUE:
            return True
        return False if text.casefold() in texts.BOOL_WORDS_FALSE else None
    if ftype == "number":
        try:
            return float(text.replace(",", "."))
        except ValueError:
            return None
    if ftype == "date":
        return {"start": text, "end": None}  # the validator parses and range-checks it
    return text  # title, rich_text, url


def to_interpretation(step: StepSpec, ctx: Context) -> Interpretation | None:
    """The step as an Interpretation, or None when the caller should ask the LLM instead."""
    if step.action == "free":
        return None
    target_key = _target_key(ctx, step.target)
    if target_key is None:
        log.info("plan step: target %r not found; asking the model", step.target)
        return None

    fields: dict[str, dict] = {k: {"status": "not_mentioned"} for k in ctx.field_keys(target_key)}
    wanted = list(step.fields)
    if step.title:
        title_key = next((k for k in ctx.field_keys(target_key)
                          if (ref := ctx.ref(k)) and ref.field_type == "title"), None)
        if title_key is None:
            return None
        fields[title_key] = {"status": "value", "value": step.title, "confidence": 1.0,
                             "source_text": step.title}
    for spec in wanted:
        field_key = _find(ctx, "field", spec.name, prefix=target_key)
        ref = ctx.ref(field_key) if field_key else None
        if ref is None:
            log.info("plan step: field %r not in %r; asking the model", spec.name, step.target)
            return None
        value = _value(ctx, field_key, ref, spec.value)
        if value is None:
            log.info("plan step: value %r does not fit %r; asking the model",
                     spec.value, spec.name)
            return None
        fields[field_key] = {"status": "value", "value": value, "confidence": 1.0,
                             "source_text": spec.value}

    candidate = {
        "target_name": ctx.target_labels.get(target_key, step.target),
        "target": target_key, "confidence": 1.0, "item": None, "item_candidates": [],
        "item_text": None, "fields": fields, "content": step.content or None,
        "search_query": None, "web_query": step.web_query or None,
        "web_media": step.web_media,
    }
    return Interpretation.model_validate({
        "intent": {"value": step.action, "confidence": 1.0},
        "candidates": [candidate], "notes": "", "clarify": None,
    })
