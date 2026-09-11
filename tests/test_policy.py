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
    (0.89, None, "EXECUTE"), (0.85, None, "EXECUTE"), (0.84, None, "CLARIFY"),
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
    # single candidate + only-low-intent-confidence -> intent_confirm, not a one-option target ask
    assert d.kind == "CLARIFY" and d.questions[0].type == "intent_confirm"


def test_required_field_missing_asks_with_options():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={"t3.f1": val("Документы")})))
    assert d.kind == "CLARIFY"
    q = d.questions[0]
    assert q.type == "field_required" and q.field_key == "t3.f2" and q.field_name == "Приоритет"
    assert [o.label for o in q.options] == ["A", "B", "C"] and q.options[0].key == "t3.f2.o1"
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={
        "t3.f1": val("x"), "t3.f2": {"status": "explicit_null"}})))
    assert d.kind == "CLARIFY" and d.questions[0].type == "field_required"


def test_required_not_checked_for_update():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t3", 0.95, item="t3.i1",
                                              fields={"t3.f4": val("t3.f4.o3")})))
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
        "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
        "t3.f3": val({"start": "2026-09-14", "end": None}, conf)})))
    assert d.kind == expect
    if expect == "CLARIFY":
        q = d.questions[0]
        assert q.type == "date"
        assert q.proposed == {"start": "2026-09-14", "end": None, "granularity": "date"}


@pytest.mark.parametrize("conf,expect", [(0.75, "EXECUTE"), (0.74, "CLARIFY")])
def test_field_confidence_boundary(conf, expect):
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={
        "t2.f1": val("x"), "t2.f2": val("t2.f2.o1", conf)})))
    assert d.kind == expect
    if expect == "CLARIFY":
        assert d.questions[0].type == "field_confirm"
        assert d.questions[0].proposed == {"id": "o-Rimi", "name": "Rimi"}


def test_update_item_resolution():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, item_candidates=["t2.i2", "t2.i4"],
                                              fields={"t2.f5": val(True)})))
    assert d.kind == "CLARIFY" and d.questions[0].type == "item"
    assert [o.label for o in d.questions[0].options] == ["Молоко", "Молоко овсяное"]
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, fields={"t2.f5": val(True)})))
    assert d.kind == "REJECT" and d.candidate is not None
    assert d.questions[0].type == "item_not_found"


def test_noop_update_rejected():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, item="t2.i2")))
    assert d.kind == "REJECT" and d.candidate is not None
    assert d.questions[0].type == "nothing_to_write" and d.reasons == ["nothing to write"]
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, item="t2.i2",
                                              fields={"t2.f5": val(True)})))
    assert d.kind == "EXECUTE"


def test_append_to_page_itself_and_content_required():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("append", cand(ctx, "t5", 0.95, content="текст")))
    assert d.kind == "EXECUTE" and d.candidate.item is None
    d, _ = decide(make_interp("append", cand(ctx, "t5", 0.95, item="t5.i1")))
    assert d.kind == "CLARIFY" and d.questions[0].type == "content_required"


def test_question_order():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp(
        "create",
        cand(ctx, "t3", 0.88, fields={
            "t3.f1": val("x"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5),
            "t3.f6": amb(["t3.f6.o1"], ["t3.f6.o2"]),
        }),
        cand(ctx, "t2", 0.85, fields={"t2.f1": val("x")}),
    ))
    assert [q.type for q in d.questions] == ["target", "field_required", "field_ambiguous", "date"]


def test_search_executes_without_item():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("search", cand(ctx, "t2", 0.95, search_query="Rimi")))
    assert d.kind == "EXECUTE"


def test_question_json_safe_and_stable_id():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={"t3.f1": val("Документы")})))
    q = d.questions[0]
    assert q.type == "field_required" and q.id == "field_required:t3.f2"
    assert q.model_dump_json()

    d, _ = decide(make_interp("create", cand(ctx, "t3", 0.95, fields={
        "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
        "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5)})))
    date_q = next(q for q in d.questions if q.type == "date")
    assert date_q.model_dump_json()

    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={
        "t2.f1": val("x"), "t2.f2": val("t2.f2.o1", 0.5)})))
    fc_q = next(q for q in d.questions if q.type == "field_confirm")
    assert fc_q.model_dump_json()


def test_item_not_found_question_carries_item_text():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("update", cand(ctx, "t2", 0.95, fields={"t2.f5": val(True)},
                                              item_text="овсяное молоко 2")))
    assert d.kind == "REJECT" and d.questions[0].type == "item_not_found"
    assert d.questions[0].proposed == "овсяное молоко 2"


def test_intent_confirm_single_candidate_low_intent():
    ctx, _ = ctx_and_snapshot()
    d, _ = decide(make_interp("create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("x")}),
                              intent_conf=0.5))
    assert d.kind == "CLARIFY"
    q = d.questions[0]
    assert q.type == "intent_confirm" and q.target_key == "t2" and q.proposed == "create"

    d, _ = decide(make_interp(
        "create",
        cand(ctx, "t2", 0.95, fields={"t2.f1": val("x")}),
        cand(ctx, "t3", 0.90, fields={"t3.f1": val("x"), "t3.f2": val("t3.f2.o1")}),
        intent_conf=0.5,
    ))
    assert d.kind == "CLARIFY" and d.questions[0].type == "target"
