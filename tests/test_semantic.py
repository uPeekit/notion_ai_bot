from datetime import date, datetime

from app.notion.snapshot import Option
from app.validation.semantic import DateRange, SemanticValidator
from tests.helpers import amb, cand, ctx_and_snapshot, make_interp, val

V = SemanticValidator()


def run(interp):
    ctx, snap = ctx_and_snapshot()
    return V.validate(interp, ctx, snap), ctx


def test_happy_create_types_values():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t2", fields={
        "t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1"), "t2.f4": val(2), "t2.f5": val(True),
        "t2.f6": val("зерновой")}))
    r, _ = run(interp)
    assert not r.rejected and r.issues == []
    c = r.best
    assert c.target.name == "Покупки" and c.key == "t2"
    assert c.fields["t2.f1"].value == "Молоко" and c.fields["t2.f1"].field.type == "title"
    assert c.fields["t2.f2"].value == Option("o-Rimi", "Rimi")
    assert c.fields["t2.f4"].value == 2 and c.fields["t2.f5"].value is True
    assert c.fields["t2.f3"].status == "not_mentioned" and c.fields["t2.f3"].value is None


def test_date_multi_relation_url_typing():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t3", fields={
        "t3.f1": val("Позвонить"), "t3.f3": val({"start": "2026-09-10", "end": None}),
        "t3.f5": val(["t3.f5.o2"]), "t3.f6": val(["t3.f6.o1", "t3.f6.o3", "t3.f6.o1"]),
        "t3.f7": val("https://example.com/a")}))
    r, _ = run(interp)
    c = r.best
    assert c.fields["t3.f3"].value == DateRange(date(2026, 9, 10), None)
    assert [o.name for o in c.fields["t3.f5"].value] == ["Работа"]
    assert [o.name for o in c.fields["t3.f6"].value] == ["дом", "здоровье"]  # deduped
    assert c.fields["t3.f7"].value == "https://example.com/a"


def test_datetime_gets_context_timezone():
    ctx, _ = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t3", fields={
        "t3.f1": val("x"), "t3.f3": val({"start": "2026-09-10T09:45", "end": "2026-09-10T10:30"})}))
    r, _ = run(interp)
    d = r.best.fields["t3.f3"].value
    assert isinstance(d.start, datetime) and d.start.tzinfo is not None
    assert d.start.isoformat() == "2026-09-10T09:45:00+03:00" and d.end.hour == 10


def test_type_errors_invalidate_candidate():
    ctx, _ = ctx_and_snapshot()
    bad = [
        {"t2.f4": val("two")},                                   # number
        {"t2.f5": val("yes")},                                   # checkbox
        {"t3.f3": val({"start": "завтра", "end": None})},        # date
        {"t3.f3": val({"start": "2026-09-12", "end": "2026-09-10"})},  # end < start
        {"t3.f7": val("example.com")},                           # url
        {"t2.f2": val("t2.f3.o1")},                              # option of another field
        {"t2.f2": val("Rimi")},                                  # option by name
    ]
    for fields in bad:
        target = next(iter(fields)).split(".")[0]
        title_key = "t2.f1" if target == "t2" else "t3.f1"
        r, _ = run(make_interp(
            "create", cand(ctx, target, fields={title_key: val("x"), **fields})
        ))
        assert r.rejected, fields
        assert any(i.code == "SEM_TYPE" for i in r.issues), fields


def test_unknown_keys_invalidate_candidate():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", {**cand(ctx, "t2"), "target": "t9"}))
    assert r.rejected and r.issues[0].code == "SEM_UNKNOWN_KEY"
    c = cand(ctx, "t2")
    c["fields"]["t3.f1"] = val("x")
    r, _ = run(make_interp("create", c))
    assert r.rejected and any(i.code == "SEM_UNKNOWN_KEY" for i in r.issues)
    r, _ = run(make_interp("update", cand(ctx, "t2", item="t3.i1")))
    assert r.rejected and any(i.code == "SEM_UNKNOWN_KEY" for i in r.issues)


def test_missing_field_key_treated_as_not_mentioned():
    ctx, _ = ctx_and_snapshot()
    c = cand(ctx, "t2", fields={"t2.f1": val("x")})
    del c["fields"]["t2.f6"]
    r, _ = run(make_interp("create", c))
    assert not r.rejected and r.best.fields["t2.f6"].status == "not_mentioned"


def test_unsupported_operation():
    ctx, _ = ctx_and_snapshot()
    # db has no append
    r, _ = run(make_interp("append", cand(ctx, "t2", item="t2.i1", content="x")))
    assert r.rejected and r.issues[0].code == "SEM_UNSUPPORTED_OP"
    # page has no update
    r, _ = run(make_interp("update", cand(ctx, "t5", item="t5.i1")))
    assert r.rejected


def test_intent_unknown_rejects():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("unknown", cand(ctx, "t2")))
    assert r.rejected and r.issues[0].code == "INTENT_UNKNOWN"


def test_dedupe_and_sort_candidates():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp(
        "create", cand(ctx, "t2", 0.6), cand(ctx, "t3", 0.8), cand(ctx, "t2", 0.7)
    ))
    assert [(c.key, c.confidence) for c in r.candidates] == [("t3", 0.8), ("t2", 0.7)]
    assert r.best.key == "t3" and r.second.key == "t2"


def test_items_and_candidates_resolved_and_capped():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("update", cand(ctx, "t2", item=None,
                                          item_candidates=["t2.i2", "t2.i4", "t2.i2"])))
    c = r.best
    assert c.item is None and [i.title for i in c.item_candidates] == ["Молоко", "Молоко овсяное"]
    r, _ = run(make_interp("update", cand(ctx, "t2", item="t2.i2", fields={"t2.f5": val(True)})))
    assert r.best.item.id == "b-milk"


def test_ambiguous_candidates_typed():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("create", cand(ctx, "t2", fields={
        "t2.f1": val("Сыр"), "t2.f2": amb("t2.f2.o1", "t2.f2.o2", "bogus")})))
    f = r.best.fields["t2.f2"]
    assert f.status == "ambiguous" and [o.name for o in f.candidates] == ["Rimi", "Prisma"]
    r, _ = run(make_interp(
        "create", cand(ctx, "t2", fields={"t2.f1": val("Сыр"), "t2.f2": amb("bogus")})
    ))
    assert r.best.fields["t2.f2"].status == "not_mentioned"


def test_status_explicit_null_dropped_with_issue():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp(
        "update", cand(ctx, "t3", item="t3.i1", fields={"t3.f4": {"status": "explicit_null"}})
    ))
    assert not r.rejected
    assert r.best.fields["t3.f4"].status == "not_mentioned"
    assert any(i.code == "SEM_STATUS_CLEAR" for i in r.issues)


def test_item_text_carried_and_capped():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp("update", cand(ctx, "t2", item_text="овсяное " * 700)))
    assert r.best.item_text is not None and len(r.best.item_text) == 4000
    r, _ = run(make_interp("update", cand(ctx, "t2", item_text=None)))
    assert r.best.item_text is None


def test_page_title_field_and_text_caps():
    ctx, _ = ctx_and_snapshot()
    r, _ = run(make_interp(
        "create", cand(ctx, "t5", fields={"t5.f1": val("Отпуск 2028")}, content="x" * 5000)
    ))
    c = r.best
    assert c.fields["t5.f1"].field.id == "__title__"
    assert c.fields["t5.f1"].value == "Отпуск 2028"
    assert len(c.content) == 4000
