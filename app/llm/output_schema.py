"""JSON schema for the LLM answer, specialised to one request's context keys."""

from __future__ import annotations

from app.llm.context import Context

INTENTS = ["create", "update", "append", "search", "unknown"]
STRING = {"type": "string"}
NUMBER = {"type": "number"}
CONFIDENCE = {"type": "number", "minimum": 0, "maximum": 1}
MAX_ITEM_CANDIDATES = 8
# Bounds the reasoning the model writes before it answers: one or two sentences, not an essay
# (every character is generated, and generation is where the latency is).
NOTES_MAX_CHARS = 300


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props) if required is None else required,
        "additionalProperties": False,
    }


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def value_schema(ctx: Context, field_key: str) -> dict:
    ref = ctx.ref(field_key)
    if ref is None or ref.kind != "field":
        raise KeyError(field_key)
    t = ref.field_type
    if t in ("title", "rich_text", "url"):
        return dict(STRING)
    if t == "number":
        return dict(NUMBER)
    if t == "checkbox":
        return {"type": "boolean"}
    if t == "date":
        return _obj({"start": dict(STRING), "end": _nullable(dict(STRING))})
    if t in ("select", "status"):
        return {"enum": ctx.option_keys(field_key)}
    if t in ("multi_select", "relation"):
        return {"type": "array", "items": {"enum": ctx.option_keys(field_key)}}
    raise ValueError(f"unsupported field type {t!r}")


def field_value_schema(ctx: Context, field_key: str) -> dict:
    v = value_schema(ctx, field_key)
    return {
        "anyOf": [
            _obj({"status": {"const": "not_mentioned"}}),
            _obj({"status": {"const": "explicit_null"}}),
            _obj(
                {
                    "status": {"const": "ambiguous"},
                    "candidates": {"type": "array", "items": v, "minItems": 2},
                    "source_text": dict(STRING),
                }
            ),
            _obj(
                {
                    "status": {"const": "value"},
                    "value": v,
                    "confidence": dict(CONFIDENCE),
                    "source_text": dict(STRING),
                }
            ),
        ]
    }


def candidate_schema(ctx: Context, target_key: str) -> dict:
    fields = {fk: field_value_schema(ctx, fk) for fk in ctx.field_keys(target_key)}
    items = ctx.item_keys(target_key)
    item = _nullable({"enum": items}) if items else {"type": "null"}
    item_candidates = (
        {"type": "array", "items": {"enum": items}, "maxItems": MAX_ITEM_CANDIDATES}
        if items
        else {"type": "array", "maxItems": 0}
    )
    props: dict = {}
    if ctx.name_targets:
        # Generated before the key and fixed per branch, so choosing a *name* ("TODO [database]")
        # is what selects the branch: the model decides among meaningful words instead of
        # opaque keys t1..tN, which it drifts to the first of when unsure. The key follows.
        props["target_name"] = {"const": ctx.target_labels[target_key]}
    return _obj(
        {
            **props,
            "target": {"const": target_key},
            "confidence": dict(CONFIDENCE),
            "item": item,
            "item_candidates": item_candidates,
            "item_text": _nullable(dict(STRING)),
            "fields": _obj(fields),
            "content": _nullable(dict(STRING)),
            "search_query": _nullable(dict(STRING)),
            **({"web_query": _nullable(dict(STRING))} if ctx.web_research else {}),
        }
    )


def build_schema(ctx: Context) -> dict:
    if not ctx.target_keys():
        raise ValueError("no targets in context")
    notes = {**STRING, "maxLength": NOTES_MAX_CHARS} if ctx.reasoning_first else dict(STRING)
    body = {
        "intent": _obj({"value": {"enum": INTENTS}, "confidence": dict(CONFIDENCE)}),
        "candidates": {
            "type": "array",
            "minItems": 1,
            "maxItems": 3,
            "items": {"anyOf": [candidate_schema(ctx, tk) for tk in ctx.target_keys()]},
        },
    }
    # Output is generated in property order. Last, notes could only explain a choice already
    # made — the model once wrote "a shopping item or a task" there after having picked the
    # first two targets in the list. First, it is the reasoning the choice follows from.
    if ctx.reasoning_first:
        return _obj({"notes": notes, **body})
    return _obj({**body, "notes": notes})
