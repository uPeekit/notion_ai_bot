import jsonschema
import pytest
from jsonschema import Draft7Validator

from app.interpretation.models import Interpretation
from app.llm.context import ContextBuilder
from app.llm.output_schema import build_schema, candidate_schema, value_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


@pytest.fixture
def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def test_schema_is_valid_draft7(ctx):
    Draft7Validator.check_schema(build_schema(ctx))


def test_value_schemas_by_type(ctx):
    assert value_schema(ctx, "t2.f1") == {"type": "string"}  # title
    assert value_schema(ctx, "t2.f2") == {"enum": ["t2.f2.o1", "t2.f2.o2", "t2.f2.o3"]}  # select
    assert value_schema(ctx, "t2.f4") == {"type": "number"}
    assert value_schema(ctx, "t2.f5") == {"type": "boolean"}
    date = value_schema(ctx, "t3.f3")
    assert date["properties"]["start"] == {"type": "string"} and "end" in date["properties"]
    assert value_schema(ctx, "t3.f6")["items"]["enum"] == [
        "t3.f6.o1",
        "t3.f6.o2",
        "t3.f6.o3",
    ]  # multi
    assert value_schema(ctx, "t3.f5")["items"]["enum"] == ["t3.f5.o1", "t3.f5.o2"]  # relation


def test_candidate_schema_pins_target_and_all_fields(ctx):
    c = candidate_schema(ctx, "t2")
    assert c["properties"]["target"] == {"const": "t2"}
    assert set(c["properties"]["fields"]["required"]) == set(ctx.field_keys("t2"))
    assert c["properties"]["fields"]["additionalProperties"] is False
    assert c["properties"]["item"]["anyOf"][0]["enum"] == ctx.item_keys("t2")
    home = candidate_schema(ctx, "t1")  # page with no children
    assert home["properties"]["item"] == {"type": "null"}
    assert home["properties"]["item_candidates"]["maxItems"] == 0


def test_valid_response_passes_schema_and_pydantic(ctx):
    schema = build_schema(ctx)
    raw = {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [
            {
                "target": "t2",
                "confidence": 0.9,
                "item": None,
                "item_candidates": [],
                "fields": {
                    "t2.f1": {
                        "status": "value",
                        "value": "Молоко",
                        "confidence": 0.99,
                        "source_text": "молоко",
                    },
                    "t2.f2": {
                        "status": "value",
                        "value": "t2.f2.o1",
                        "confidence": 0.9,
                        "source_text": "в Рими",
                    },
                    "t2.f3": {"status": "not_mentioned"},
                    "t2.f4": {"status": "ambiguous", "candidates": [1, 2], "source_text": "пару"},
                    "t2.f5": {"status": "explicit_null"},
                    "t2.f6": {"status": "not_mentioned"},
                },
                "content": None,
                "search_query": None,
            }
        ],
        "notes": "",
    }
    jsonschema.validate(raw, schema)
    Interpretation.model_validate(raw)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r["candidates"][0].__setitem__("target", "t9"),
        lambda r: r["candidates"][0]["fields"].__setitem__(
            "t2.f2", {"status": "value", "value": "Rimi", "confidence": 1, "source_text": ""}
        ),
        lambda r: r["candidates"][0]["fields"].pop("t2.f6"),
        lambda r: r["candidates"][0].__setitem__("item", "t3.i1"),
        lambda r: r["candidates"][0].__setitem__("url", "http://x"),
        lambda r: r.__setitem__("candidates", []),
    ],
)
def test_invalid_responses_fail_schema(ctx, mutate):
    schema = build_schema(ctx)
    raw = {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [
            {
                "target": "t2",
                "confidence": 0.9,
                "item": None,
                "item_candidates": [],
                "fields": {k: {"status": "not_mentioned"} for k in ctx.field_keys("t2")},
                "content": None,
                "search_query": None,
            }
        ],
        "notes": "",
    }
    mutate(raw)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, schema)


def test_schema_size_reasonable(ctx):
    import json

    assert len(json.dumps(build_schema(ctx))) < 60_000
