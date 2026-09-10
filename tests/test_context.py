import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.llm.context import PAGE_TITLE_FIELD_ID, ContextBuilder
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def build(**kw):
    return ContextBuilder("Europe/Tallinn", **kw).build(sample_snapshot(), now=SAMPLE_NOW)


def test_keys_and_payload_shape():
    ctx = build()
    p = ctx.payload
    assert p["now"] == "2026-09-09T18:00+03:00"
    assert p["tz"] == "Europe/Tallinn" and p["weekday"] == "среда"
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


def test_json_is_compact_and_unicode():
    ctx = build()
    s = ctx.json()
    assert '"name":"Покупки"' in s and "\\u" not in s
    assert json.loads(s)["targets"][0]["name"] == "Дом"
