import pytest
from pydantic import ValidationError

from app.interpretation.models import Candidate, Interpretation, Value

RAW = {
    "intent": {"value": "create", "confidence": 0.97},
    "candidates": [
        {
            "target": "t1",
            "confidence": 0.95,
            "item": None,
            "item_candidates": [],
            "fields": {
                "t1.f1": {"status": "value", "value": "Молоко", "confidence": 0.99,
                          "source_text": "молоко"},
                "t1.f2": {"status": "not_mentioned"},
                "t1.f3": {"status": "ambiguous", "candidates": ["t1.f3.o1", "t1.f3.o2"],
                          "source_text": "рими"},
                "t1.f4": {"status": "explicit_null"},
            },
            "content": None,
            "search_query": None,
        }
    ],
    "notes": "",
}


def test_roundtrip():
    interp = Interpretation.model_validate(RAW)
    assert interp.intent.value == "create"
    c = interp.candidates[0]
    assert isinstance(c.fields["t1.f1"], Value)
    assert c.fields["t1.f2"].status == "not_mentioned"
    assert c.fields["t1.f3"].candidates == ["t1.f3.o1", "t1.f3.o2"]
    assert Interpretation.model_validate_json(interp.model_dump_json()) == interp


def test_unknown_status_rejected():
    bad = {**RAW}
    bad["candidates"] = [{**RAW["candidates"][0], "fields": {"t1.f1": {"status": "maybe"}}}]
    with pytest.raises(ValidationError):
        Interpretation.model_validate(bad)


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        Interpretation.model_validate({**RAW, "intent": {"value": "create", "confidence": 1.2}})


def test_candidates_min_one():
    with pytest.raises(ValidationError):
        Interpretation.model_validate({**RAW, "candidates": []})


def test_extra_keys_forbidden():
    with pytest.raises(ValidationError):
        Candidate.model_validate({**RAW["candidates"][0], "url": "http://evil"})


def test_best_and_second():
    two = {**RAW, "candidates": [RAW["candidates"][0],
                                 {**RAW["candidates"][0], "target": "t2", "confidence": 0.5}]}
    interp = Interpretation.model_validate(two)
    assert interp.best.target == "t1"
    assert interp.second.target == "t2"
    assert Interpretation.model_validate(RAW).second is None
