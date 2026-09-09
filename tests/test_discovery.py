from datetime import UTC, datetime, timedelta

import pytest

from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta
from app.notion.discovery import Discovery
from app.notion.errors import NotionUnavailable
from tests.fakes import FakeNotionProvider


def rich(text):
    return [{"plain_text": text}]


def page(id, title, parent, edited="2026-09-01T00:00:00.000Z", extra=None):
    props = {"title": {"id": "title", "type": "title", "title": rich(title)}}
    props.update(extra or {})
    return {"object": "page", "id": id, "url": f"https://notion.so/{id}",
            "last_edited_time": edited, "parent": parent, "properties": props}


@pytest.fixture
def fake():
    f = FakeNotionProvider()
    f.search_results = [
        page("home", "Дом", {"type": "workspace", "workspace": True}),
        page("ideas", "Идеи", {"type": "page_id", "page_id": "home"}),
        page("trip", "Отпуск", {"type": "page_id", "page_id": "ideas"}),
        {"object": "data_source", "id": "ds-buy",
         "parent": {"type": "database_id", "database_id": "db-buy"}},
        {"object": "data_source", "id": "ds-shops",
         "parent": {"type": "database_id", "database_id": "db-shops"}},
        page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
    ]
    f.databases = {
        "db-buy": {"id": "db-buy", "parent": {"type": "page_id", "page_id": "home"}},
        "db-shops": {"id": "db-shops", "parent": {"type": "workspace", "workspace": True}},
    }
    f.data_sources = {
        "ds-buy": {
            "id": "ds-buy", "title": rich("Покупки"), "description": rich("Из Notion"),
            "url": "https://notion.so/ds-buy", "parent": {"database_id": "db-buy"},
            "properties": {
                "Название": {"id": "title", "type": "title"},
                "Магазин": {"id": "shop", "type": "select",
                            "select": {"options": [{"id": "o1", "name": "Rimi"},
                                                   {"id": "o2", "name": "Prisma"}]}},
                "Куплено": {"id": "done", "type": "checkbox"},
                "Где": {"id": "rel", "type": "relation",
                        "relation": {"data_source_id": "ds-shops"}},
                "Формула": {"id": "fx", "type": "formula"},
            },
        },
        "ds-shops": {
            "id": "ds-shops", "title": rich("Магазины"), "description": [],
            "url": "https://notion.so/ds-shops", "parent": {"database_id": "db-shops"},
            "properties": {"Name": {"id": "title", "type": "title"}},
        },
    }
    f.items = {
        "ds-buy": [
            page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
            page("row2", "Молоко", {"type": "data_source_id", "data_source_id": "ds-buy"},
                 edited="2026-09-02T00:00:00.000Z",
                 extra={"Куплено": {"type": "checkbox", "checkbox": True}}),
        ],
        "ds-shops": [page("s1", "Rimi Hyper", {"type": "data_source_id",
                                               "data_source_id": "ds-shops"})],
    }
    return f


@pytest.fixture
def disco(fake, tmp_path):
    return Discovery(fake, Descriptions(tmp_path / "t.yaml"), items_per_target=10, ttl_s=60)


async def test_databases_discovered(disco):
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.kind == "database"
    assert buy.name == "Покупки"
    assert buy.path == "Дом / Покупки"
    assert buy.database_id == "db-buy"
    assert buy.parent_page_id == "home"
    assert buy.description == "Из Notion"
    assert {f.id: f.type for f in buy.fields} == {"title": "title", "shop": "select",
                                                  "done": "checkbox", "rel": "relation",
                                                  "fx": "readonly"}
    assert buy.field("title").required is True
    assert [o.name for o in buy.field("shop").options] == ["Rimi", "Prisma"]
    assert buy.field("rel").relation_data_source_id == "ds-shops"
    assert [o.name for o in buy.field("rel").options] == ["Rimi Hyper"]
    assert [(i.id, i.title, i.hint) for i in buy.items] == [("row2", "Молоко", "Куплено"),
                                                            ("row1", "Хлеб", None)]
    assert buy.items[0].url == "https://notion.so/row2"
    assert buy.operations == frozenset({"create", "update", "search"})
    assert snap.target("ds-shops").path == "Магазины"


async def test_pages_discovered_with_children(disco):
    snap = await disco.refresh()
    ideas = snap.target("ideas")
    assert ideas.kind == "page"
    assert ideas.path == "Дом / Идеи"
    assert ideas.parent_page_id == "home"
    assert [i.title for i in ideas.items] == ["Отпуск"]
    assert ideas.operations == frozenset({"create_page", "append", "search"})
    assert snap.target("row1") is None  # rows are not page targets
    assert snap.target("home").path == "Дом"


async def test_descriptions_override_and_required(disco, tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(description="Мой список",
                                 fields={"shop": FieldMeta(description="Сеть", required=True)})})
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.description == "Мой список"
    assert buy.field("shop").required is True
    assert buy.field("shop").description == "Сеть"
    on_disk = d.load()
    assert on_disk["ideas"].name == "Идеи"
    assert on_disk["ds-buy"].fields["done"].name == "Куплено"


async def test_cache_ttl_and_invalidate(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    b = await disco.get()
    assert a is b
    t["now"] += timedelta(seconds=61)
    c = await disco.get()
    assert c is not a
    disco.invalidate()
    assert (await disco.get()) is not c
    assert sum(1 for c in fake.calls if c[0] == "search") == 3


async def test_stale_snapshot_on_failure(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    fake.fail_search = NotionUnavailable()
    t["now"] += timedelta(minutes=30)
    assert (await disco.get()) is a
    t["now"] += timedelta(minutes=31)
    with pytest.raises(NotionUnavailable):
        await disco.get()


async def test_invalidate_keeps_stale_snapshot_on_failure(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    disco.invalidate()
    fake.fail_search = NotionUnavailable()
    b = await disco.get()
    assert b is a
    fake.fail_search = None
    c = await disco.get()
    assert c is not a


async def test_missing_relation_target_gives_empty_options(disco, fake):
    del fake.data_sources["ds-shops"]
    fake.search_results = [r for r in fake.search_results if r["id"] != "ds-shops"]
    snap = await disco.refresh()
    assert snap.target("ds-buy").field("rel").options == []
