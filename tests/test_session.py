"""session_from_decision flattens a validated Decision into a JSON-serialisable PendingSession
keyed by Notion ids, and SessionStore persists it over AuditStore. The central rule under test:
no context key (t1, t1.f2, t1.f2.o3, t1.i4, ...) may appear anywhere in a serialised session
EXCEPT inside the persisted Question object, whose field_key/target_key/options[].key are part
of that model on purpose (Task 3 re-resolves them against a fresh context after a rediscovery)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.audit.store import AuditStore
from app.conversation.session import (
    MAX_QUESTIONS,
    AnswerOption,
    PendingCandidate,
    PendingField,
    PendingSession,
    SessionStore,
    session_from_decision,
)
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from tests.helpers import amb, cand, ctx_and_snapshot, make_interp, val

T = Thresholds(intent_min=0.85, target_min=0.85, target_margin=0.10, field_min=0.75, date_min=0.80)
NOW = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)

# Context-key shapes the ContextBuilder assigns positionally (app/llm/context.py): t1, t1.f2,
# t1.f2.o3, t1.i4, plus policy.py's own item ("t3.item:<page id>") and ambiguous-field
# ("t3.f2#0") key variants. None of these may survive outside the Question object.
CONTEXT_KEY = re.compile(
    r'"t\d+(\.f\d+(\.o\d+)?(#\d+)?|\.i\d+|\.item:[^"]*)?"'
)


def decide(interp, t=T):
    ctx, snap = ctx_and_snapshot()
    result = SemanticValidator().validate(interp, ctx, snap)
    return Policy(t).evaluate(result), result, ctx


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path / "t.sqlite")
    s.migrate()
    yield s
    s.close()


def _field_required_session(asked: list[str] | None = None, chat_id: int = 42) -> PendingSession:
    """t3 = "Задачи" (ds-todo); the required select field "Приоритет" (prio) is missing, so
    Policy asks field_required with options t3.f2.o1..o3 (labels A/B/C, backed by Notion option
    ids o-A/o-B/o-C). AnswerOptions are hand-built here since resolver.py (Task 3) does not
    exist yet; its opaque ids (o0, o1, ...) are exactly what it will produce."""
    decision, result, ctx = decide(
        make_interp("create", cand(ctx_and_snapshot()[0], "t3", 0.95,
                                    fields={"t3.f1": val("Подготовить документы")}))
    )
    q = decision.questions[0]
    options = [
        AnswerOption(id=f"o{i}", label=opt.label, field_id="prio", option_id=f"o-{opt.label}")
        for i, opt in enumerate(q.options)
    ]
    return session_from_decision(
        chat_id=chat_id, event_id=7, text="создай задачу подготовить документы", result=result,
        decision=decision, options=options, now=NOW, ttl_s=600, asked=asked or [],
    )


def test_field_required_session_keeps_notion_ids_not_context_keys():
    s = _field_required_session()
    assert s.candidates[0].target_id == "ds-todo"  # Notion Target.id, not "t3"
    prio = next(f for f in s.candidates[0].fields if f.field_id == "prio")
    assert prio.name == "Приоритет" and prio.type == "select" and prio.status == "not_mentioned"
    assert {o.option_id for o in s.options} == {"o-A", "o-B", "o-C"}
    assert {o.field_id for o in s.options} == {"prio"}


def test_no_context_key_outside_the_question_object():
    s = _field_required_session()
    dump = s.model_dump(mode="json")
    question_blob = json.dumps(dump.pop("question"), ensure_ascii=False)
    rest_blob = json.dumps(dump, ensure_ascii=False)

    # Prove the pattern is not vacuous: the Question really does carry context keys.
    assert CONTEXT_KEY.search(question_blob), "fixture is not exercising a real context key"
    # ...and nothing outside Question does.
    assert CONTEXT_KEY.search(rest_blob) is None


def test_round_trip_through_json_preserves_every_field_including_question():
    s = _field_required_session(asked=["field_required:t3.f2"])
    restored = PendingSession.model_validate_json(s.model_dump_json())
    assert restored == s
    assert restored.question == s.question
    assert restored.created_at == s.created_at and restored.created_at.tzinfo is not None


def test_round_trip_through_session_store(store):
    s = _field_required_session()
    ss = SessionStore(store)
    ss.save(s)
    restored = ss.get(42, NOW + timedelta(seconds=1))
    assert restored == s


def test_expired_session_not_returned_and_deleted(store):
    s = _field_required_session()
    ss = SessionStore(store)
    ss.save(s)
    assert ss.get(42, s.expires_at + timedelta(seconds=1)) is None
    # deleted, not just filtered: even a not-yet-expired lookup now finds nothing
    assert store.get_session(42, NOW) is None


def test_corrupt_payload_is_deleted_and_returns_none(store):
    store.save_session(1, "{not json", NOW + timedelta(minutes=5))
    ss = SessionStore(store)
    assert ss.get(1, NOW) is None
    assert store.get_session(1, NOW) is None


def test_drop_removes_session(store):
    s = _field_required_session()
    ss = SessionStore(store)
    ss.save(s)
    ss.drop(42)
    assert ss.get(42, NOW) is None


def sweep(ss: SessionStore, store: AuditStore, now) -> list[PendingSession]:
    """The two halves of the inbox sweeper, in the order Orchestrator.flush_expired_sessions
    drives them: scan for candidates, then pop each one (the orchestrator does it under that
    chat's own lock, which is why the loop lives there and not in SessionStore)."""
    popped = [ss.pop_expired_one(chat_id, now) for chat_id, _ in store.expired_sessions(now)]
    return [s for s in popped if s is not None]


def test_pop_expired_returns_only_expired_sessions(store):
    ss = SessionStore(store)
    expired = _field_required_session(chat_id=1)
    store.save_session(1, expired.model_dump_json(), NOW - timedelta(seconds=1))
    fresh = _field_required_session(chat_id=2)
    store.save_session(2, fresh.model_dump_json(), NOW + timedelta(minutes=5))
    popped = sweep(ss, store, NOW)
    assert [p.chat_id for p in popped] == [1]
    assert store.get_session(1, NOW - timedelta(minutes=1)) is None
    assert store.get_session(2, NOW) is not None


def test_pop_expired_does_not_swallow_a_session_renewed_between_scan_and_delete(store):
    """TOCTOU guard: the sweeper first scans (expired_sessions), then deletes per row
    (pop_expired_session). If a save_session renewal for the same chat_id lands in that gap —
    plausible in a store built for concurrent access (RLock, check_same_thread=False) — the
    renewed, still-live session must survive and remain readable, not be swallowed by a delete
    that was decided against a stale scan result. Deterministic simulation: monkeypatch
    expired_sessions so the renewal happens, as a side effect, right after the scan it feeds."""
    ss = SessionStore(store)
    stale = _field_required_session(chat_id=1)
    store.save_session(1, stale.model_dump_json(), NOW - timedelta(seconds=1))
    real_expired_sessions = store.expired_sessions
    renewed = _field_required_session(chat_id=1, asked=["field_required:t3.f2"])

    def scan_then_race_a_renewal(now):
        rows = real_expired_sessions(now)
        ss.save(renewed)  # same call the orchestrator makes on a real answer
        return rows

    store.expired_sessions = scan_then_race_a_renewal
    try:
        popped = sweep(ss, store, NOW)
    finally:
        store.expired_sessions = real_expired_sessions

    assert popped == []  # the stale scan result must not delete the now-live session
    restored = ss.get(1, NOW)
    assert restored == renewed
    assert restored.expires_at > NOW
    assert restored.asked == ["field_required:t3.f2"]


def test_is_exhausted_after_three_answers():
    s = _field_required_session(asked=["a", "b"])
    assert not s.is_exhausted
    s2 = _field_required_session(asked=["a", "b", "c"])
    assert s2.is_exhausted
    assert MAX_QUESTIONS == 3


def test_best_returns_first_candidate():
    s = _field_required_session()
    assert s.best is s.candidates[0]


def test_nothing_to_write_reject_persists_and_round_trips():
    """nothing_to_write is a REJECT, not a CLARIFY, but DATA_MODEL.md §4 has it carry the
    candidate and a single Question on purpose so Plan 3 can act on it without re-running the
    LLM (here: the orchestrator will just offer free text). session_from_decision must accept
    it."""
    decision, result, _ = decide(
        make_interp("update", cand(ctx_and_snapshot()[0], "t2", 0.95, item="t2.i2"))
    )
    assert decision.kind == "REJECT" and decision.questions[0].type == "nothing_to_write"
    s = session_from_decision(
        chat_id=1, event_id=1, text="обнови молоко", result=result, decision=decision,
        options=[], now=NOW, ttl_s=600, asked=[],
    )
    assert s.question.type == "nothing_to_write"
    restored = PendingSession.model_validate_json(s.model_dump_json())
    assert restored == s


def test_execute_decision_still_raises():
    ctx, _ = ctx_and_snapshot()
    decision, result, _ = decide(
        make_interp("create", cand(ctx, "t2", 0.95, fields={"t2.f1": val("Молоко")}))
    )
    assert decision.kind == "EXECUTE"
    with pytest.raises(ValueError):
        session_from_decision(
            chat_id=1, event_id=1, text="х", result=result, decision=decision,
            options=[], now=NOW, ttl_s=600, asked=[],
        )


def test_extra_field_forbidden_on_every_model():
    with pytest.raises(ValidationError):
        PendingField(field_id="x", name="n", type="select", status="value", bogus=1)
    with pytest.raises(ValidationError):
        PendingCandidate(target_id="x", confidence=1.0, item_page_id=None, item_text=None,
                          content=None, search_query=None, fields=[], bogus=1)
    with pytest.raises(ValidationError):
        AnswerOption(id="o0", label="A", bogus=1)
    with pytest.raises(ValidationError):
        PendingSession(**_field_required_session().model_dump(), bogus=1)


def test_ambiguous_field_persists_typed_candidates_not_keys():
    ctx, _ = ctx_and_snapshot()
    decision, result, _ = decide(make_interp(
        "create",
        cand(ctx, "t2", 0.95, fields={
            "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2"),
        }),
    ))
    s = session_from_decision(chat_id=1, event_id=1, text="сыр", result=result,
                              decision=decision, options=[], now=NOW, ttl_s=600, asked=[])
    f2 = next(f for f in s.candidates[0].fields if f.field_id == "shop")
    assert f2.status == "ambiguous"
    assert f2.candidates == [{"id": "o-Rimi", "name": "Rimi"}, {"id": "o-Prisma", "name": "Prisma"}]
    blob = json.dumps(s.model_dump(mode="json")["candidates"])
    assert CONTEXT_KEY.search(blob) is None


def test_a_plan_rides_along_in_the_session_json():
    from app.conversation.plan import PlanState
    from app.conversation.session import PendingSession

    s = _field_required_session()
    from app.conversation.plan import StepSpec

    plan = PlanState(goal="g", steps=[StepSpec(text="a"), StepSpec(text="b")]).model_dump()
    back = PendingSession.model_validate_json(s.model_copy(update={"plan": plan})
                                              .model_dump_json())
    assert [s.text for s in PlanState.model_validate(back.plan).steps] == ["a", "b"]
