"""Task 3: the answer resolver. `apply_answer` turns a button press back into an updated
PendingSession purely from stored ids (no LLM, no context, no snapshot); `rebuild_candidate` /
result_from_session turn a PendingSession back into a fresh ValidationResult against a possibly
-rediscovered workspace; `options_for` is the only place that turns Question.options (context
keys) into id-based AnswerOptions, and it runs while the generating Context is still in hand;
`next_question` walks a Decision's questions and returns the first one not yet in `asked`."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from app.conversation.resolver import (
    apply_answer,
    next_question,
    options_for,
    rebuild_candidate,
    result_from_session,
)
from app.conversation.session import _pending_candidate, session_from_decision
from app.llm.context import ContextBuilder
from app.notion.snapshot import Option
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from tests.helpers import amb, cand, ctx_and_snapshot, make_interp, val
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

T = Thresholds(intent_min=0.85, target_min=0.85, target_margin=0.10, field_min=0.75, date_min=0.80)
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)


def decide(interp, t=T):
    ctx, snap = ctx_and_snapshot()
    result = SemanticValidator().validate(interp, ctx, snap)
    return Policy(t).evaluate(result), result, ctx, snap


def build_session(interp, *, asked=None, chat_id=42, t=T):
    """Full round-trip: decide -> options_for -> session_from_decision, exactly as the
    orchestrator (Task 5) will drive it. session_from_decision only accepts kind=="CLARIFY";
    the item_not_found/nothing_to_write REJECT-with-a-question cases carry exactly the same
    shape (a candidate plus one Question) and the orchestrator is expected to persist them the
    same way, so reshape the Decision's kind here rather than duplicate session_from_decision."""
    decision, result, ctx, snap = decide(interp, t)
    q = decision.questions[0]
    options = options_for(q, decision.candidate, result, ctx)
    clarify = decision if decision.kind == "CLARIFY" else replace(decision, kind="CLARIFY")
    session = session_from_decision(
        chat_id=chat_id, event_id=1, text="msg", result=result, decision=clarify,
        options=options, now=NOW, ttl_s=600, asked=asked or [],
    )
    return session, decision, result, ctx, snap


# --------------------------------------------------------------------------------------------
# options_for: ids only, never a context key
# --------------------------------------------------------------------------------------------

def _no_context_key(options):
    import re
    blob = "".join(
        f"{o.id}|{o.label}|{o.target_id}|{o.item_page_id}|{o.field_id}|{o.option_id}|{o.value}"
        for o in options
    )
    assert re.search(r"\bt\d+(\.f\d+|\.i\d+)?\b", blob) is None, blob


def test_options_for_target_yields_ids_only():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create",
        cand(ctx, "t2", 0.90, fields={"t2.f1": val("x")}),
        cand(ctx, "t3", 0.81, fields={"t3.f1": val("x"), "t3.f2": val("t3.f2.o1")}),
    ))
    q = d.questions[0]
    assert q.type == "target"
    opts = options_for(q, d.candidate, result, ctx)
    _no_context_key(opts)
    by_id = {o.id: o for o in opts}
    assert by_id["o0"].target_id == "ds-buy"
    assert by_id["o1"].target_id == "ds-todo"
    assert {"cancel", "inbox"} <= by_id.keys()


def test_options_for_item_yields_page_ids_only():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "update", cand(ctx, "t2", 0.95, item_candidates=["t2.i2", "t2.i4"],
                        fields={"t2.f5": val(True)})
    ))
    q = d.questions[0]
    assert q.type == "item"
    opts = options_for(q, d.candidate, result, ctx)
    _no_context_key(opts)
    by_id = {o.id: o for o in opts}
    assert by_id["o0"].item_page_id == "b-milk"
    assert by_id["o1"].item_page_id == "b-milk2"


def test_options_for_field_required_yields_field_and_option_ids():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create", cand(ctx, "t3", 0.95, fields={"t3.f1": val("Документы")})
    ))
    q = d.questions[0]
    assert q.type == "field_required"
    opts = options_for(q, d.candidate, result, ctx)
    _no_context_key(opts)
    by_id = {o.id: o for o in opts}
    assert by_id["o0"].field_id == "prio" and by_id["o0"].option_id == "o-A"
    assert by_id["o1"].option_id == "o-B" and by_id["o2"].option_id == "o-C"


def test_options_for_field_ambiguous_yields_typed_values():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create", cand(ctx, "t2", 0.95, fields={
            "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2"),
        })
    ))
    q = d.questions[0]
    assert q.type == "field_ambiguous"
    opts = options_for(q, d.candidate, result, ctx)
    _no_context_key(opts)
    by_id = {o.id: o for o in opts}
    assert by_id["o0"].field_id == "shop" and by_id["o0"].value == {"id": "o-Rimi", "name": "Rimi"}
    assert by_id["o1"].value == {"id": "o-Prisma", "name": "Prisma"}


def test_options_for_date_and_field_confirm_have_confirm_and_other():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create", cand(ctx, "t3", 0.95, fields={
            "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5),
        })
    ))
    q = next(q for q in d.questions if q.type == "date")
    opts = options_for(q, d.candidate, result, ctx)
    ids = {o.id for o in opts}
    assert {"confirm", "other", "cancel", "inbox"} <= ids
    assert next(o for o in opts if o.id == "confirm").field_id == "due"


def test_options_for_item_not_found_carries_title_field_id():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "update", cand(ctx, "t2", 0.95, fields={"t2.f5": val(True)}, item_text="сыр")
    ))
    assert d.kind == "REJECT" and d.questions[0].type == "item_not_found"
    opts = options_for(d.questions[0], d.candidate, result, ctx)
    add_new = next(o for o in opts if o.id == "add_new")
    assert add_new.field_id == "title"


def test_options_for_intent_confirm():
    ctx, _ = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("x")}), intent_conf=0.5
    ))
    assert d.questions[0].type == "intent_confirm"
    opts = options_for(d.questions[0], d.candidate, result, ctx)
    assert {"confirm", "cancel", "inbox"} <= {o.id for o in opts}


# --------------------------------------------------------------------------------------------
# apply_answer: one case per answer-table row
# --------------------------------------------------------------------------------------------

def test_apply_answer_target():
    s, *_ = build_session(make_interp(
        "create",
        cand(ctx_and_snapshot()[0], "t2", 0.90, fields={"t2.f1": val("x")}),
        cand(ctx_and_snapshot()[0], "t3", 0.81, fields={
            "t3.f1": val("x"), "t3.f2": val("t3.f2.o1")}),
    ))
    assert s.question.type == "target"
    updated, verb = apply_answer(s, "o1")
    assert verb is None
    assert len(updated.candidates) == 1
    assert updated.candidates[0].target_id == "ds-todo"
    assert updated.candidates[0].confidence == 1.0
    assert updated.asked == ["target:"]


def test_apply_answer_intent_confirm():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t2", 0.95, fields={"t2.f1": val("x")}),
        intent_conf=0.5,
    ))
    assert s.question.type == "intent_confirm"
    updated, verb = apply_answer(s, "confirm")
    assert verb is None
    assert updated.intent_confidence == 1.0
    assert updated.asked == ["intent_confirm:"]


def test_apply_answer_item():
    s, *_ = build_session(make_interp(
        "update", cand(ctx_and_snapshot()[0], "t2", 0.95, item_candidates=["t2.i2", "t2.i4"],
                        fields={"t2.f5": val(True)})
    ))
    assert s.question.type == "item"
    updated, verb = apply_answer(s, "o1")
    assert verb is None
    assert updated.candidates[0].item_page_id == "b-milk2"
    assert updated.asked == ["item:"]


def test_apply_answer_item_not_found_add_new():
    s, *_ = build_session(make_interp(
        "update", cand(ctx_and_snapshot()[0], "t2", 0.95, fields={"t2.f5": val(True)},
                        item_text="овсяное молоко 2")
    ))
    assert s.question.type == "item_not_found"
    updated, verb = apply_answer(s, "add_new")
    assert verb is None
    assert updated.intent == "create"
    title = next(f for f in updated.candidates[0].fields if f.field_id == "title")
    assert title.status == "value" and title.value == "овсяное молоко 2" and title.confidence == 1.0
    assert updated.candidates[0].item_page_id is None
    assert updated.asked == ["item_not_found:"]


def test_apply_answer_field_required():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={"t3.f1": val("Документы")})
    ))
    assert s.question.type == "field_required"
    updated, verb = apply_answer(s, "o1")
    assert verb is None
    prio = next(f for f in updated.candidates[0].fields if f.field_id == "prio")
    assert prio.status == "value" and prio.value == {"id": "o-B", "name": "B"}
    assert prio.confidence == 1.0
    assert updated.asked == ["field_required:prio"]


def test_apply_answer_field_ambiguous():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t2", 0.95, fields={
            "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2")})
    ))
    assert s.question.type == "field_ambiguous"
    updated, verb = apply_answer(s, "o1")
    assert verb is None
    shop = next(f for f in updated.candidates[0].fields if f.field_id == "shop")
    assert shop.status == "value" and shop.value == {"id": "o-Prisma", "name": "Prisma"}
    assert shop.confidence == 1.0 and shop.candidates == []
    assert updated.asked == ["field_ambiguous:shop"]


def test_apply_answer_date_confirm():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5)})
    ))
    assert s.question.type == "date"
    due = next(f for f in s.candidates[0].fields if f.field_id == "due")
    assert due.confidence == 0.5
    updated, verb = apply_answer(s, "confirm")
    assert verb is None
    due2 = next(f for f in updated.candidates[0].fields if f.field_id == "due")
    assert due2.confidence == 1.0 and due2.value == due.value
    assert updated.asked == ["date:due"]


def test_apply_answer_field_confirm_confirm():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t2", 0.95, fields={
            "t2.f1": val("x"), "t2.f2": val("t2.f2.o1", 0.5)})
    ))
    assert s.question.type == "field_confirm"
    updated, verb = apply_answer(s, "confirm")
    assert verb is None
    shop = next(f for f in updated.candidates[0].fields if f.field_id == "shop")
    assert shop.confidence == 1.0
    assert updated.asked == ["field_confirm:shop"]


def test_apply_answer_date_other_is_free_text_and_untouched():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5)})
    ))
    assert s.question.type == "date"
    updated, verb = apply_answer(s, "other")
    assert verb == "free_text"
    assert updated == s


def test_apply_answer_field_confirm_other_is_free_text_and_untouched():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t2", 0.95, fields={
            "t2.f1": val("x"), "t2.f2": val("t2.f2.o1", 0.5)})
    ))
    updated, verb = apply_answer(s, "other")
    assert verb == "free_text"
    assert updated == s


def test_apply_answer_cancel_and_inbox_any_question_type():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={"t3.f1": val("Документы")})
    ))
    updated, verb = apply_answer(s, "cancel")
    assert verb == "cancel" and updated == s
    updated, verb = apply_answer(s, "inbox")
    assert verb == "inbox" and updated == s


def test_apply_answer_unknown_option_id_is_a_safe_noop():
    s, *_ = build_session(make_interp(
        "create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={"t3.f1": val("Документы")})
    ))
    updated, verb = apply_answer(s, "o99")
    assert verb is None and updated == s


# --------------------------------------------------------------------------------------------
# next_question: skip already-answered keys
# --------------------------------------------------------------------------------------------

def test_next_question_skips_answered_and_returns_none_when_exhausted():
    ctx, _ = ctx_and_snapshot()
    d, _, _, _ = decide(make_interp(
        "create",
        cand(ctx, "t3", 0.88, fields={
            "t3.f1": val("x"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.5),
        }),
        cand(ctx, "t2", 0.85, fields={"t2.f1": val("x")}),
    ))
    assert [q.type for q in d.questions] == ["target", "field_required", "date"]
    q = next_question(d, [], ctx)
    assert q.type == "target"
    q = next_question(d, ["target:"], ctx)
    assert q.type == "field_required"
    q = next_question(d, ["target:", "field_required:prio"], ctx)
    assert q.type == "date"
    q = next_question(d, ["target:", "field_required:prio", "date:due"], ctx)
    assert q is None


# --------------------------------------------------------------------------------------------
# rebuild_candidate / result_from_session against an unchanged snapshot
# --------------------------------------------------------------------------------------------

def test_rebuild_candidate_roundtrips_against_unchanged_snapshot():
    ctx, snap = ctx_and_snapshot()
    _, result, _, _ = decide(make_interp(
        "create", cand(ctx, "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.9),
        })
    ))
    pc = _pending_candidate(result.best)
    issues = []
    vc = rebuild_candidate(pc, snap, ctx, issues)
    assert vc is not None and issues == []
    assert vc.key == "t3" and vc.target.id == "ds-todo"
    prio = vc.fields["t3.f2"]
    assert prio.status == "value" and prio.value == Option("o-A", "A")
    due = vc.fields["t3.f3"]
    assert due.status == "value" and due.value.start.isoformat() == "2026-09-14"


def test_result_from_session_orders_by_confidence_and_carries_intent():
    ctx, snap = ctx_and_snapshot()
    d, result, _, _ = decide(make_interp(
        "create",
        cand(ctx, "t3", 0.88, fields={
            "t3.f1": val("x"), "t3.f2": val("t3.f2.o1"),
            "t3.f3": val({"start": "2026-09-14", "end": None}, 0.9),
        }),
        cand(ctx, "t2", 0.85, fields={"t2.f1": val("x")}),
    ))
    session = session_from_decision(
        chat_id=1, event_id=1, text="x", result=result, decision=d, options=[],
        now=NOW, ttl_s=600, asked=[],
    )
    rebuilt = result_from_session(session, snap, ctx)
    assert rebuilt.intent == "create" and rebuilt.issues == []
    assert [c.key for c in rebuilt.candidates] == ["t3", "t2"]


# --------------------------------------------------------------------------------------------
# rebuild against a *changed* snapshot
# --------------------------------------------------------------------------------------------

def _renamed_target_snapshot():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    renamed = replace(todo, name="Дела")
    targets = [renamed if t.id == "ds-todo" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _deleted_option_snapshot():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    prio = todo.field("prio")
    new_prio = replace(prio, options=[o for o in prio.options if o.name != "A"])
    new_fields = [new_prio if f.id == "prio" else f for f in todo.fields]
    new_todo = replace(todo, fields=new_fields)
    targets = [new_todo if t.id == "ds-todo" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _renamed_option_snapshot():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    prio = todo.field("prio")
    new_prio = replace(prio, options=[replace(o, name="Срочно") if o.id == "o-A" else o
                                      for o in prio.options])
    new_fields = [new_prio if f.id == "prio" else f for f in todo.fields]
    new_todo = replace(todo, fields=new_fields)
    targets = [new_todo if t.id == "ds-todo" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _dropped_item_candidate_snapshot():
    snap = sample_snapshot()
    buy = snap.target("ds-buy")
    new_buy = replace(buy, items=[i for i in buy.items if i.id != "b-milk2"])
    targets = [new_buy if t.id == "ds-buy" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _deleted_field_snapshot():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    new_fields = [f for f in todo.fields if f.id != "prio"]
    new_todo = replace(todo, fields=new_fields)
    targets = [new_todo if t.id == "ds-todo" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _deleted_item_snapshot():
    snap = sample_snapshot()
    buy = snap.target("ds-buy")
    new_items = [i for i in buy.items if i.id != "b-milk"]
    new_buy = replace(buy, items=new_items)
    targets = [new_buy if t.id == "ds-buy" else t for t in snap.targets]
    return replace(snap, targets=targets)


def _deleted_target_snapshot():
    snap = sample_snapshot()
    targets = [t for t in snap.targets if t.id != "ds-todo"]
    return replace(snap, targets=targets)


def _rebuild_from_original(fresh_snapshot, interp):
    _, result, _, _ = decide(interp)
    pc = _pending_candidate(result.best)
    fresh_ctx = ContextBuilder("Europe/Tallinn").build(fresh_snapshot, now=SAMPLE_NOW)
    issues = []
    vc = rebuild_candidate(pc, fresh_snapshot, fresh_ctx, issues)
    return vc, issues


def test_rebuild_after_renamed_target_still_resolves_by_id():
    vc, issues = _rebuild_from_original(
        _renamed_target_snapshot(),
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1")})),
    )
    assert vc is not None and issues == []
    assert vc.target.id == "ds-todo" and vc.target.name == "Дела"
    assert vc.fields["t3.f2"].value == Option("o-A", "A")


def test_rebuild_after_deleted_option_drops_only_that_field():
    vc, issues = _rebuild_from_original(
        _deleted_option_snapshot(),
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1")})),
    )
    assert vc is not None
    assert "t3.f2" not in vc.fields
    assert "t3.f1" in vc.fields and vc.fields["t3.f1"].value == "Документы"
    assert any(i.code == "SEM_UNKNOWN_KEY" for i in issues)


def test_rebuild_after_renamed_option_keeps_the_field_with_its_fresh_name():
    """The headline claim of session.py's docstring and DATA_MODEL.md §4: a stored answer is a
    Notion id, so renaming the option the user picked must survive the round-trip and come back
    carrying the *new* label — not the one that was on the button."""
    vc, issues = _rebuild_from_original(
        _renamed_option_snapshot(),
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1")})),
    )
    assert vc is not None and issues == []
    assert vc.fields["t3.f2"].value == Option("o-A", "Срочно")


def test_rebuild_restores_item_candidates_by_page_id():
    """Policy only offers the `item` question while item_candidates is non-empty, so they have
    to survive a round-trip; the ones whose page is gone drop out, like every other id."""
    interp = make_interp("update", cand(
        ctx_and_snapshot()[0], "t2", 0.95, item_candidates=["t2.i2", "t2.i4"],
        item_text="молоко", fields={"t2.f5": val(True)}))
    _, result, _, snap = decide(interp)
    pc = _pending_candidate(result.best)
    assert pc.item_candidates == ["b-milk", "b-milk2"]  # page ids, never "t2.i2"

    ctx, _ = ctx_and_snapshot()
    issues = []
    vc = rebuild_candidate(pc, snap, ctx, issues)
    assert [i.id for i in vc.item_candidates] == ["b-milk", "b-milk2"]

    vc, _ = _rebuild_from_original(_dropped_item_candidate_snapshot(), interp)
    assert [i.id for i in vc.item_candidates] == ["b-milk"]


def test_rebuild_after_deleted_field_drops_that_field_without_issue():
    """Deleting "prio" (not just one of its options) shifts ContextBuilder's positional
    numbering for every field after it, so check by Notion field id rather than by the stale
    "t3.f2" context key — that key now names whatever field slid into its old slot."""
    vc, issues = _rebuild_from_original(
        _deleted_field_snapshot(),
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1")})),
    )
    assert vc is not None
    assert not any(f.field.id == "prio" for f in vc.fields.values())
    assert any(f.field.id == "title" and f.value == "Документы" for f in vc.fields.values())
    assert issues == []


def test_rebuild_after_deleted_item_leaves_item_none():
    vc, issues = _rebuild_from_original(
        _deleted_item_snapshot(),
        make_interp("update", cand(ctx_and_snapshot()[0], "t2", 0.95, item="t2.i2",
                                    fields={"t2.f5": val(True)})),
    )
    assert vc is not None and vc.item is None


def test_rebuild_after_deleted_target_returns_none():
    vc, issues = _rebuild_from_original(
        _deleted_target_snapshot(),
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95, fields={
            "t3.f1": val("Документы"), "t3.f2": val("t3.f2.o1")})),
    )
    assert vc is None


# --------------------------------------------------------------------------------------------
# end-to-end: two-question sequence converges to EXECUTE
# --------------------------------------------------------------------------------------------

def test_two_question_sequence_converges_to_execute():
    ctx, snap = ctx_and_snapshot()
    interp = make_interp(
        "create",
        cand(ctx, "t3", 0.80, fields={"t3.f1": val("Документы")}),
        cand(ctx, "t2", 0.60, fields={"t2.f1": val("x")}),
    )
    d, result, ctx, snap = decide(interp)
    assert d.kind == "CLARIFY" and d.questions[0].type == "target"
    q0 = d.questions[0]
    options0 = options_for(q0, d.candidate, result, ctx)
    session = session_from_decision(
        chat_id=1, event_id=1, text="создай задачу подготовить документы", result=result,
        decision=d, options=options0, now=NOW, ttl_s=600, asked=[],
    )

    # answer target -> t3 (ds-todo)
    target_opt = next(o for o in options0 if o.target_id == "ds-todo")
    session, verb = apply_answer(session, target_opt.id)
    assert verb is None and session.asked == ["target:"]

    # re-evaluate against the (unchanged) workspace and Policy
    result2 = result_from_session(session, snap, ctx)
    decision2 = Policy(T).evaluate(result2)
    assert decision2.kind == "CLARIFY"
    q1 = next_question(decision2, session.asked, ctx)
    assert q1.type == "field_required" and q1.field_key == "t3.f2"

    options1 = options_for(q1, decision2.candidate, result2, ctx)
    session = session.model_copy(update={"question": q1, "options": options1})
    field_opt = next(o for o in options1 if o.option_id == "o-A")
    session, verb = apply_answer(session, field_opt.id)
    assert verb is None
    assert session.asked == ["target:", "field_required:prio"]

    result3 = result_from_session(session, snap, ctx)
    decision3 = Policy(T).evaluate(result3)
    assert decision3.kind == "EXECUTE"
    assert next_question(decision3, session.asked, ctx) is None
