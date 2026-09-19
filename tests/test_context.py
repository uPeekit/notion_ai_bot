import json
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.llm.context import PAGE_TITLE_FIELD_ID, ContextBuilder, build_calendar
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def build(**kw):
    return ContextBuilder("Europe/Tallinn", **kw).build(sample_snapshot(), now=SAMPLE_NOW)


def test_keys_and_payload_shape():
    ctx = build()
    p = ctx.payload
    assert p["now"] == "2026-09-09T18:00+03:00"
    assert p["tz"] == "Europe/Tallinn" and p["weekday"] == "среда"
    assert p["calendar"]["сегодня"] == "2026-09-09 (среда)"
    assert [t["key"] for t in p["targets"]] == ["t1", "t2", "t3", "t4", "t5"]
    buy = p["targets"][1]
    assert buy["name"] == "Покупки" and buy["kind"] == "database" and buy["path"] == "Дом / Покупки"
    assert buy["ops"] == ["create", "search", "update"]
    names = [f["name"] for f in buy["fields"]]
    # readonly dropped
    assert names == ["Название", "Магазин", "Категория", "Количество", "Куплено", "Заметка"]
    shop = buy["fields"][1]
    assert shop["key"] == "t2.f2" and shop["type"] == "select"
    assert shop["options"] == {"t2.f2.o1": "Rimi", "t2.f2.o2": "Prisma", "t2.f2.o3": "Maxima"}
    assert buy["fields"][0]["required"] is True and "required" not in shop
    assert buy["items"] == {"t2.i1": "Хлеб", "t2.i2": "Молоко (Куплено)", "t2.i3": "Яйца",
                            "t2.i4": "Молоко овсяное"}


def test_empty_option_field_dropped_and_relation_options_present():
    ctx = build()
    todo = ctx.payload["targets"][2]
    names = [f["name"] for f in todo["fields"]]
    assert "Пустой список" not in names
    project = next(f for f in todo["fields"] if f["name"] == "Проект")
    assert project["type"] == "relation" and list(project["options"].values()) == ["Дом", "Работа"]


def test_page_targets_get_synthetic_title_and_children():
    ctx = build()
    ideas = ctx.payload["targets"][4]
    assert ideas["kind"] == "page" and ideas["ops"] == ["append", "create", "search"]
    assert ideas["fields"] == [{
        "key": "t5.f1", "name": "Заголовок", "type": "title", "required": True,
        "description": "Название новой подстраницы",
    }]
    assert ideas["children"] == {"t5.i1": "Отпуск 2027", "t5.i2": "Книги"}
    assert ctx.ref("t5.f1").field_id == PAGE_TITLE_FIELD_ID


def test_reverse_lookups():
    ctx = build()
    assert ctx.target_key("ds-buy") == "t2"
    assert ctx.ref("t2").target_id == "ds-buy" and ctx.ref("t2").kind == "target"
    assert ctx.ref("t2.f2.o1").option_id == "o-Rimi" and ctx.ref("t2.f2.o1").name == "Rimi"
    assert ctx.ref("t2.i2").item_id == "b-milk"
    assert ctx.field_keys("t2") == ["t2.f1", "t2.f2", "t2.f3", "t2.f4", "t2.f5", "t2.f6"]
    assert ctx.option_keys("t2.f2") == ["t2.f2.o1", "t2.f2.o2", "t2.f2.o3"]
    assert ctx.item_keys("t5") == ["t5.i1", "t5.i2"]
    assert ctx.ref("nope") is None
    assert ctx.target_keys() == ["t1", "t2", "t3", "t4", "t5"]


def test_reverse_key_lookups_by_notion_id():
    ctx = build()
    assert ctx.field_key("ds-buy", "shop") == "t2.f2"
    assert ctx.option_key("ds-buy", "shop", "o-Rimi") == "t2.f2.o1"
    assert ctx.item_key("ds-buy", "b-milk") == "t2.i2"
    assert ctx.field_key("ds-buy", "nope") is None
    assert ctx.option_key("ds-buy", "shop", "nope") is None
    assert ctx.item_key("ds-buy", "nope") is None


def test_items_cap_and_no_ids_or_urls_in_payload():
    ctx = build(items_per_target=2)
    assert list(ctx.payload["targets"][1]["items"]) == ["t2.i1", "t2.i2"]
    text = ctx.json()
    assert "ds-buy" not in text and "notion.so" not in text and "b-milk" not in text


def test_pending_and_now_default(monkeypatch):
    ctx = ContextBuilder("Europe/Tallinn").build(sample_snapshot(), pending={"question": "Куда?"})
    assert ctx.payload["pending"] == {"question": "Куда?"}
    assert ctx.now.tzinfo is not None
    ctx2 = ContextBuilder("UTC").build(
        sample_snapshot(), now=datetime(2026, 1, 5, 12, tzinfo=ZoneInfo("UTC"))
    )
    assert ctx2.payload["weekday"] == "понедельник"


def test_build_raises_on_naive_now():
    with pytest.raises(ValueError, match="timezone-aware"):
        ContextBuilder("Europe/Tallinn").build(sample_snapshot(), now=datetime(2026, 1, 5, 12))


def test_json_is_compact_and_unicode():
    ctx = build()
    s = ctx.json()
    assert '"name":"Покупки"' in s and "\\u" not in s
    assert json.loads(s)["targets"][0]["name"] == "Дом"


def test_build_calendar_wednesday():
    cal = build_calendar(SAMPLE_NOW)  # 2026-09-09 is a Wednesday
    assert cal["сегодня"] == "2026-09-09 (среда)"
    assert cal["завтра"] == "2026-09-10 (четверг)"
    assert cal["послезавтра"] == "2026-09-11 (пятница)"
    assert cal["ближайшие дни"] == {
        "четверг": "2026-09-10", "пятница": "2026-09-11", "суббота": "2026-09-12",
        "воскресенье": "2026-09-13", "понедельник": "2026-09-14", "вторник": "2026-09-15",
        "среда": "2026-09-16",
    }
    assert cal["через неделю"] == "2026-09-16"
    assert cal["через две недели"] == "2026-09-23"
    assert cal["следующая неделя"] == "2026-09-14 … 2026-09-20"


def test_build_calendar_sunday_next_week_starts_tomorrow():
    sunday = datetime(2026, 9, 13, 12, tzinfo=ZoneInfo("Europe/Tallinn"))  # Sunday
    cal = build_calendar(sunday)
    assert cal["завтра"] == "2026-09-14 (понедельник)"
    assert cal["следующая неделя"] == "2026-09-14 … 2026-09-20"


def _with_local_only(target_id: str):
    snap = sample_snapshot()
    targets = [replace(t, local_only=t.id == target_id) for t in snap.targets]
    return replace(snap, targets=targets)


def test_cloud_payload_hides_a_local_only_targets_description_and_items():
    ctx = ContextBuilder().build(_with_local_only("ds-buy"), now=SAMPLE_NOW)
    tk = ctx.target_key("ds-buy")
    assert ctx.local_only == frozenset({tk})
    local = next(t for t in ctx.payload["targets"] if t["key"] == tk)
    cloud = next(t for t in json.loads(ctx.json(cloud=True))["targets"] if t["key"] == tk)
    assert local["description"] and local["items"]
    assert "description" not in cloud and "items" not in cloud
    assert [f["key"] for f in cloud["fields"]] == [f["key"] for f in local["fields"]]
    assert not any("description" in f for f in cloud["fields"])
    # everything else, and the local view itself, unchanged
    others = [t for t in ctx.payload["targets"] if t["key"] != tk]
    assert [t for t in json.loads(ctx.json(cloud=True))["targets"] if t["key"] != tk] == others
    assert json.loads(ctx.json()) == ctx.payload


def test_cloud_payload_is_the_payload_when_nothing_is_local_only():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    assert ctx.local_only == frozenset() and ctx.json(cloud=True) == ctx.json()


def _with(target_id: str, **changes):
    snap = sample_snapshot()
    return replace(snap, targets=[replace(t, **changes) if t.id == target_id else t
                                  for t in snap.targets])


def test_hidden_targets_are_left_out_of_the_context():
    ctx = ContextBuilder().build(_with("ds-buy", hidden=True), now=SAMPLE_NOW)
    assert ctx.target_key("ds-buy") is None
    assert "Покупки" not in ctx.json()
    assert len(ctx.target_keys()) == len(sample_snapshot().targets) - 1


def test_described_options_are_listed_beside_the_options():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    tags = todo.field("tags")
    described = [replace(o, description="всё по дому") if o.name == "дом" else o
                 for o in tags.options]
    fields = [replace(f, options=described) if f.id == "tags" else f for f in todo.fields]
    ctx = ContextBuilder().build(_with("ds-todo", fields=fields), now=SAMPLE_NOW)
    fk = ctx.field_key("ds-todo", "tags")
    entry = next(f for t in ctx.payload["targets"] for f in t["fields"] if f["key"] == fk)
    home = ctx.option_key("ds-todo", "tags", "o-дом")
    assert entry["option_descriptions"] == {home: "всё по дому"}
    assert entry["options"][home] == "дом"
    # a field with no described option carries no empty block
    prio = next(f for t in ctx.payload["targets"] for f in t["fields"]
                if f["key"] == ctx.field_key("ds-todo", "prio"))
    assert "option_descriptions" not in prio


def test_local_only_hides_option_descriptions_from_the_cloud():
    snap = sample_snapshot()
    todo = snap.target("ds-todo")
    fields = [replace(f, options=[replace(o, description="x") for o in f.options])
              if f.id == "tags" else f for f in todo.fields]
    ctx = ContextBuilder().build(_with("ds-todo", fields=fields, local_only=True), now=SAMPLE_NOW)
    assert "option_descriptions" in ctx.json()
    assert "option_descriptions" not in ctx.json(cloud=True)


def test_workspace_note_is_read_for_every_build_and_omitted_when_empty():
    note = ["Все задачи — в TODO."]
    builder = ContextBuilder(note=lambda: note[0])
    assert builder.build(sample_snapshot(), now=SAMPLE_NOW).payload["workspace_note"] == note[0]
    note[0] = ""
    assert "workspace_note" not in builder.build(sample_snapshot(), now=SAMPLE_NOW).payload
    assert "workspace_note" not in ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW).payload
