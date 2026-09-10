import pytest

from app.commands.executor import Executor, UndoRecord
from app.commands.models import (
    AppendBlocks,
    CreateItem,
    CreatePage,
    PropertyWrite,
    Search,
    UpdateItem,
)
from app.notion.errors import NotionError
from tests.fakes import FakeNotionProvider


def pw(pid, name, type, value):
    return PropertyWrite(property_id=pid, property_name=name, type=type, value=value)


@pytest.fixture
def fake():
    return FakeNotionProvider()


async def test_create_item(fake):
    ex = Executor(fake)
    r = await ex.run(
        CreateItem(
            data_source_id="ds",
            target_name="Покупки",
            properties=[
                pw("title", "Название", "title", "Молоко"),
                pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"}),
            ],
        )
    )
    assert r.page_id == "new-page" and r.url == "https://notion.so/new-page"
    assert [(w.name, w.value) for w in r.written] == [
        ("Название", "Молоко"),
        ("Магазин", {"id": "o1", "name": "Rimi"}),
    ]
    assert r.undo == UndoRecord(kind="archive", page_id="new-page")
    call = fake.calls[-1]
    assert call[0] == "create_page" and call[1] == {
        "type": "data_source_id",
        "data_source_id": "ds",
    }
    assert call[2]["shop"] == {"select": {"id": "o1"}}


async def test_update_item_records_previous_values(fake):
    fake.pages["p1"] = {
        "id": "p1",
        "url": "https://notion.so/p1",
        "properties": {
            "Куплено": {"id": "done", "type": "checkbox", "checkbox": False},
            "Магазин": {
                "id": "shop",
                "type": "select",
                "select": {"id": "o2", "name": "Prisma"},
            },
            "Название": {
                "id": "title",
                "type": "title",
                "title": [{"plain_text": "Молоко"}],
            },
        },
    }
    ex = Executor(fake)
    r = await ex.run(
        UpdateItem(
            page_id="p1",
            target_name="Покупки",
            item_title="Молоко",
            properties=[
                pw("done", "Куплено", "checkbox", True),
                pw("shop", "Магазин", "select", None),
            ],
        )
    )
    assert r.page_id == "p1" and r.url == "https://notion.so/p1"
    assert r.undo.kind == "restore" and r.undo.page_id == "p1"
    assert r.undo.properties == {
        "done": {"checkbox": False},
        "shop": {"select": {"id": "o2"}},
    }
    assert fake.calls[-1] == (
        "update_page",
        "p1",
        {"done": {"checkbox": True}, "shop": {"select": None}},
        None,
    )


async def test_create_page_and_append(fake):
    ex = Executor(fake)
    r = await ex.run(
        CreatePage(parent_page_id="pg", target_name="Идеи", title="Отпуск", body=["a"])
    )
    assert r.undo.kind == "archive" and fake.calls[-1][1] == {
        "type": "page_id",
        "page_id": "pg",
    }
    assert fake.calls[-1][3][0]["paragraph"]["rich_text"][0]["text"]["content"] == "a"
    r = await ex.run(
        AppendBlocks(page_id="pg", target_name="Идеи", page_title="Идеи", paragraphs=["x", "y"])
    )
    assert r.block_ids == ["blk-0", "blk-1"]
    assert r.undo == UndoRecord(kind="delete_blocks", block_ids=["blk-0", "blk-1"])
    assert r.page_id == "pg"


async def test_search_in_data_source_and_workspace(fake):
    fake.data_sources["ds"] = {"id": "ds"}
    fake.items["ds"] = [
        {
            "id": f"r{i}",
            "url": f"https://notion.so/r{i}",
            "properties": {"N": {"type": "title", "title": [{"plain_text": f"Товар {i}"}]}},
        }
        for i in range(25)
    ]
    ex = Executor(fake)
    r = await ex.run(
        Search(data_source_id="ds", target_name="Покупки", title_property="N", query="Товар")
    )
    assert len(r.hits) == 20 and r.hits[0].title == "Товар 0" and r.hits[0].page_id == "r0"
    assert r.undo is None
    fake.search_results = [
        {
            "object": "page",
            "id": "pg",
            "url": "https://notion.so/pg",
            "properties": {"title": {"type": "title", "title": [{"plain_text": "Идеи"}]}},
        },
        {"object": "data_source", "id": "ds"},
    ]
    r = await ex.run(
        Search(data_source_id=None, target_name="Идеи", title_property=None, query="Идеи")
    )
    assert [h.title for h in r.hits] == ["Идеи"]
    assert fake.calls[-1] == ("search", "Идеи", "page")


async def test_undo_kinds(fake):
    ex = Executor(fake)
    await ex.undo(UndoRecord(kind="archive", page_id="p"))
    assert fake.calls[-1] == ("update_page", "p", None, True)
    await ex.undo(UndoRecord(kind="restore", page_id="p", properties={"done": {"checkbox": False}}))
    assert fake.calls[-1] == ("update_page", "p", {"done": {"checkbox": False}}, None)
    await ex.undo(UndoRecord(kind="delete_blocks", block_ids=["a", "b"]))
    assert fake.calls[-2:] == [("delete_block", "a"), ("delete_block", "b")]


async def test_no_undo_when_created_page_has_no_id(fake):
    async def no_id(parent, properties, children=None):
        return {"properties": properties}

    fake.create_page = no_id
    r = await Executor(fake).run(CreateItem(data_source_id="ds", target_name="x", properties=[]))
    assert r.undo is None


async def test_no_undo_when_append_returns_no_block_ids(fake):
    async def no_ids(block_id, children):
        return {"results": [{}]}

    fake.append_blocks = no_ids
    r = await Executor(fake).run(
        AppendBlocks(page_id="p", target_name="x", page_title="x", paragraphs=["a"])
    )
    assert r.undo is None and r.block_ids == []


async def test_partial_capture_is_flagged(fake):
    fake.pages["p1"] = {
        "id": "p1",
        "properties": {
            "Формула": {
                "id": "fx",
                "type": "formula",
                "formula": {"type": "number", "number": 1},
            },
            "Куплено": {"id": "done", "type": "checkbox", "checkbox": False},
        },
    }
    r = await Executor(fake).run(
        UpdateItem(
            page_id="p1",
            target_name="x",
            item_title="y",
            properties=[
                pw("done", "Куплено", "checkbox", True),
                pw("fx", "Формула", "formula", 2),
            ],
        )
    )
    assert r.undo.partial is True and r.undo.properties == {"done": {"checkbox": False}}
    assert [w.name for w in r.written] == ["Куплено"]  # formula dropped by the mapper


async def test_notion_errors_propagate(fake):
    async def boom(*a, **k):
        raise NotionError(400, "validation_error", "bad")

    fake.create_page = boom
    with pytest.raises(NotionError):
        await Executor(fake).run(CreateItem(data_source_id="ds", target_name="x", properties=[]))
