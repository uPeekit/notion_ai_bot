from datetime import UTC, datetime

from app.notion.snapshot import (
    DB_OPERATIONS,
    PAGE_OPERATIONS,
    WRITABLE_TYPES,
    Field,
    Item,
    Option,
    Target,
    WorkspaceSnapshot,
)


def make_db(id="ds1", name="Покупки"):
    return Target(
        id=id, kind="database", name=name, path=name, description="", parent_page_id=None,
        database_id="db1",
        fields=[
            Field(id="title", name="Название", type="title", required=True, options=[],
                  relation_data_source_id=None, description=""),
            Field(id="shop", name="Магазин", type="select", required=False,
                  options=[Option("o1", "Rimi")], relation_data_source_id=None, description=""),
        ],
        items=[Item(id="p1", title="Хлеб", hint=None, last_edited=datetime.now(UTC), url="")],
        operations=DB_OPERATIONS, url="https://notion.so/ds1",
    )


def make_page(id="pg1", name="Идеи"):
    return Target(
        id=id, kind="page", name=name, path=name, description="", parent_page_id=None,
        database_id=None, fields=[], items=[], operations=PAGE_OPERATIONS, url="https://notion.so/pg1",
    )


def test_snapshot_lookup_and_partition():
    snap = WorkspaceSnapshot(fetched_at=datetime.now(UTC), targets=[make_db(), make_page()])
    assert snap.target("ds1").name == "Покупки"
    assert snap.target("nope") is None
    assert [t.id for t in snap.databases()] == ["ds1"]
    assert [t.id for t in snap.pages()] == ["pg1"]


def test_target_field_lookup():
    t = make_db()
    assert t.field("shop").name == "Магазин"
    assert t.field("x") is None
    assert t.title_field().id == "title"


def test_writable_types_fixed():
    assert WRITABLE_TYPES == frozenset(
        {"title", "rich_text", "select", "multi_select", "status", "date", "checkbox",
         "number", "url", "relation"}
    )
