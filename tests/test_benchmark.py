from pathlib import Path

from app.interpretation.models import Interpretation
from app.llm.context import ContextBuilder
from tools.benchmark_llm import CaseResult, load_cases, resolve_value, score_case, summarize
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

CASES = Path("tests/fixtures/ru_cases.yaml")


def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def interp(target="t2", intent="create", item=None, item_candidates=(), fields=None, content=None,
           search_query=None, extra_candidates=()):
    c = ctx()
    base = {k: {"status": "not_mentioned"} for k in c.field_keys(target)}
    base.update(fields or {})
    cands = [{"target": target, "confidence": 0.9, "item": item,
              "item_candidates": list(item_candidates),
              "fields": base, "content": content, "search_query": search_query}]
    for t in extra_candidates:
        cands.append({"target": t, "confidence": 0.5, "item": None, "item_candidates": [],
                      "fields": {k: {"status": "not_mentioned"} for k in c.field_keys(t)},
                      "content": None, "search_query": None})
    return Interpretation.model_validate({"intent": {"value": intent, "confidence": 0.9},
                                          "candidates": cands, "notes": ""})


def val(v, conf=0.9):
    return {"status": "value", "value": v, "confidence": conf, "source_text": ""}


def test_cases_load_and_reference_known_names():
    cases = load_cases(CASES)
    assert len(cases) >= 40
    names = {r.name for r in ctx().keys.values()}
    known_intents = {"create", "update", "append", "search", "unknown"}
    for case in cases:
        for name in [case.get("target"), *case.get("targets_any", []), case.get("item")]:
            if name is not None:
                assert name in names, (case["id"], name)
        expected = {
            **case.get("fields", {}), **case.get("statuses", {}), **case.get("max_confidence", {})
        }
        for fname in expected:
            assert fname in names, (case["id"], fname)
        intents = case["intent_any"] if "intent_any" in case else [case.get("intent")]
        for intent in intents:
            assert intent in known_intents, (case["id"], intent)


def test_resolve_value_maps_option_keys():
    c = ctx()
    assert resolve_value(c, "t2.f2", "t2.f2.o1") == "Rimi"
    assert resolve_value(c, "t3.f6", ["t3.f6.o1", "t3.f6.o3"]) == ["дом", "здоровье"]
    assert resolve_value(c, "t2.f1", "Молоко") == "Молоко"


def test_score_happy_case():
    case = {"id": "x", "text": "купи молоко в Рими", "intent": "create", "target": "Покупки",
            "fields": {"Название": "молоко", "Магазин": "Rimi"},
            "statuses": {"Категория": "not_mentioned"}}
    r = score_case(case, interp(fields={"t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1")}), ctx())
    assert r.valid and r.intent_ok and r.target_ok and r.fields_ok and r.all_ok and r.item_ok


def test_score_field_mismatch_and_status():
    case = {"id": "x", "text": "", "intent": "create", "target": "Покупки",
            "fields": {"Магазин": "Prisma"}}
    r = score_case(case, interp(fields={"t2.f2": val("t2.f2.o1")}), ctx())
    assert r.target_ok and not r.fields_ok and not r.all_ok and "Магазин" in r.error
    case = {"id": "y", "text": "", "intent": "create", "target": "Покупки",
            "statuses": {"Магазин": ["ambiguous"]}}
    assert not score_case(case, interp(fields={"t2.f2": val("t2.f2.o1")}), ctx()).fields_ok


def test_score_date_prefix_wildcard_bool_number():
    case = {"id": "d", "text": "", "intent": "create", "target": "Задачи",
            "fields": {"Срок": "2026-09-10", "Задача": "*", "Приоритет": "A"}}
    i = interp(target="t3", fields={"t3.f3": val({"start": "2026-09-10T09:45", "end": None}),
                                    "t3.f1": val("Позвонить"), "t3.f2": val("t3.f2.o1")})
    assert score_case(case, i, ctx()).fields_ok
    case = {"id": "b", "text": "", "intent": "update", "target": "Покупки", "item": "Яйца",
            "fields": {"Куплено": True, "Количество": 6}}
    i = interp(intent="update", item="t2.i3", fields={"t2.f5": val(True), "t2.f4": val(6)})
    assert score_case(case, i, ctx()).all_ok


def test_score_ambiguity_items_content_search_max_conf():
    c = ctx()
    case = {"id": "a", "text": "", "intent": "create", "targets_any": ["Покупки", "Задачи"],
            "min_candidates": 2}
    assert score_case(case, interp(extra_candidates=["t3"]), c).all_ok
    assert not score_case(case, interp(), c).all_ok
    case = {"id": "i", "text": "", "intent": "update", "target": "Покупки",
            "item_candidates_min": 2}
    assert score_case(case, interp(intent="update", item_candidates=["t2.i2", "t2.i4"]), c).item_ok
    assert not score_case(case, interp(intent="update", item="t2.i2"), c).item_ok
    case = {"id": "c", "text": "", "intent": "append", "target": "Идеи", "content": "*"}
    assert score_case(case, interp(target="t5", intent="append", content="текст"), c).all_ok
    assert not score_case(case, interp(target="t5", intent="append", content=None), c).all_ok
    case = {"id": "m", "text": "", "intent": "create", "target": "Задачи",
            "max_confidence": {"Срок": 0.85}}
    low = interp(target="t3", fields={"t3.f3": val({"start": "2026-09-14", "end": None}, 0.6)})
    high = interp(target="t3", fields={"t3.f3": val({"start": "2026-09-14", "end": None}, 0.95)})
    assert score_case(case, low, c).fields_ok
    assert not score_case(case, high, c).fields_ok


def test_unknown_intent_case_needs_no_target():
    case = {"id": "u", "text": "", "intent": "unknown"}
    assert score_case(case, interp(intent="unknown"), ctx()).all_ok


def test_summarize():
    rs = [CaseResult("a", True, True, True, True, True, True, True, 100, "", False),
          CaseResult("b", True, True, False, True, False, False, True, 300, "t", False),
          CaseResult("c", False, False, False, False, False, False, False, 50, "invalid", True)]
    s = summarize("m", rs)
    assert s.n == 3 and s.valid == 2 / 3 and s.intent == 2 / 3
    assert s.target == 1 / 3 and s.all == 1 / 3
    assert s.safe == 2 / 3
    assert s.wrong == 1  # only "c" has wrong_value=True; wrong is now summed directly
    assert s.p50_ms == 100 and s.p95_ms == 300


def test_score_case_intent_any_accepts_any_listed_intent():
    case = {"id": "ia", "text": "", "intent_any": ["create", "search"],
            "targets_any": ["Покупки", "Задачи"]}
    assert score_case(case, interp(intent="search"), ctx()).intent_ok
    assert score_case(case, interp(intent="create"), ctx()).intent_ok
    assert not score_case(case, interp(intent="update"), ctx()).intent_ok


def test_score_case_safe_ok_deferred_vs_confidently_wrong_value():
    case = {"id": "sd", "text": "", "intent": "create", "target": "Задачи",
            "fields": {"Срок": "2026-09-10"}}
    amb_fields = {"t3.f3": {"status": "ambiguous",
                            "candidates": [{"start": "2026-09-10", "end": None}],
                            "source_text": "завтра"}}
    r_amb = score_case(case, interp(target="t3", fields=amb_fields), ctx())
    assert r_amb.safe_ok and not r_amb.all_ok and not r_amb.wrong_value

    wrong_fields = {"t3.f3": val({"start": "2026-09-15", "end": None})}
    r_wrong = score_case(case, interp(target="t3", fields=wrong_fields), ctx())
    assert not r_wrong.safe_ok and not r_wrong.all_ok and r_wrong.wrong_value

    s = summarize("m", [r_amb, r_wrong])
    assert s.wrong == 1


def test_score_case_scalar_expected_matches_single_item_list_field():
    # relation/multi_select fields resolve to a list even when only one value is set; a case
    # written with a bare scalar expectation ("Работа") must still match ["Работа"].
    case = {"id": "rel", "text": "", "intent": "create", "target": "Задачи",
            "fields": {"Проект": "Работа"}}
    i = interp(target="t3", fields={"t3.f5": val(["t3.f5.o2"])})
    assert score_case(case, i, ctx()).fields_ok


def test_score_case_statuses_only_wrong_and_safe():
    # model returns a confident value where only not_mentioned was allowed -> wrong (unsafe)
    case_wrong = {"id": "sw", "text": "", "intent": "create", "target": "Задачи",
                  "statuses": {"Приоритет": "not_mentioned"}}
    r_wrong = score_case(
        case_wrong, interp(target="t3", fields={"t3.f2": val("t3.f2.o1")}), ctx()
    )
    assert not r_wrong.fields_ok and not r_wrong.safe_ok and r_wrong.wrong_value

    # model defers (not_mentioned) where a value was expected -> safe failure
    case_safe = {"id": "ss", "text": "", "intent": "create", "target": "Задачи",
                 "statuses": {"Приоритет": "value"}}
    r_safe = score_case(case_safe, interp(target="t3"), ctx())
    assert not r_safe.fields_ok and r_safe.safe_ok and not r_safe.wrong_value
