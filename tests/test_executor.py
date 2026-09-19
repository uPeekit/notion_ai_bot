import pytest

from app.commands.builder import build_command
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
from app.validation.semantic import SemanticValidator
from tests.fakes import FakeNotionProvider
from tests.helpers import cand, ctx_and_snapshot, make_interp, val


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


async def test_restore_undo_none_when_nothing_captured(fake):
    fake.pages["p1"] = {"id": "p1", "url": "https://notion.so/p1", "properties": {}}
    r = await Executor(fake).run(
        UpdateItem(
            page_id="p1",
            target_name="x",
            item_title="y",
            properties=[pw("done", "Куплено", "checkbox", True)],
        )
    )
    assert r.undo is None


async def test_undo_delete_blocks_survives_already_deleted(fake):
    calls: list[str] = []

    async def flaky(block_id):
        calls.append(block_id)
        if block_id == "a":
            raise NotionError(404, "object_not_found", "x")
        return {"id": block_id}

    fake.delete_block = flaky
    await Executor(fake).undo(UndoRecord(kind="delete_blocks", block_ids=["a", "b"]))
    assert calls == ["a", "b"]


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


async def test_mapper_dropped_property_does_not_flag_partial(fake):
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
    assert r.undo.partial is False  # formula was never written
    assert r.undo.properties == {"done": {"checkbox": False}}
    assert [w.name for w in r.written] == ["Куплено"]


async def test_uncapturable_written_property_flags_partial(fake):
    # page lacks the "shop" property entirely, so its previous value cannot be captured
    fake.pages["p1"] = {
        "id": "p1",
        "properties": {"Куплено": {"id": "done", "type": "checkbox", "checkbox": False}},
    }
    r = await Executor(fake).run(
        UpdateItem(
            page_id="p1",
            target_name="x",
            item_title="y",
            properties=[
                pw("done", "Куплено", "checkbox", True),
                pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"}),
            ],
        )
    )
    assert r.undo.partial is True
    assert r.undo.properties == {"done": {"checkbox": False}}
    assert [w.name for w in r.written] == ["Куплено", "Магазин"]


async def test_create_item_written_excludes_mapper_dropped(fake):
    r = await Executor(fake).run(
        CreateItem(
            data_source_id="ds",
            target_name="x",
            properties=[
                pw("title", "Название", "title", "Молоко"),
                pw("fx", "Формула", "formula", 1),
            ],
        )
    )
    assert [w.name for w in r.written] == ["Название"]


async def test_notion_errors_propagate(fake):
    async def boom(*a, **k):
        raise NotionError(400, "validation_error", "bad")

    fake.create_page = boom
    with pytest.raises(NotionError):
        await Executor(fake).run(CreateItem(data_source_id="ds", target_name="x", properties=[]))


# ---- Markdown bodies, batching, images ----------------------------------------------------


class FakeImages:
    def __init__(self, hosted: dict[str, str | None]):
        self.hosted = hosted
        self.asked: list[str] = []

    async def host(self, url):
        self.asked.append(url)
        return self.hosted.get(url)


async def test_markdown_append_writes_formatted_blocks(fake):
    r = await Executor(fake).run(AppendBlocks(
        page_id="pg", target_name="Идеи", page_title="Идеи", markdown=True,
        paragraphs=["## План", "- [ ] верстак", "- свет"]))
    blocks = fake.calls[-1][2]
    assert [b["type"] for b in blocks] == ["heading_2", "to_do", "bulleted_list_item"]
    assert r.undo.block_ids == ["blk-0", "blk-1", "blk-2"]


async def test_plain_append_stays_verbatim(fake):
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="Идеи", page_title="Идеи",
                                          paragraphs=["- не список, а текст"]))
    assert fake.calls[-1][2][0]["type"] == "paragraph"


async def test_long_append_is_sent_in_batches_of_100(fake):
    lines = [f"- пункт {i}" for i in range(230)]
    r = await Executor(fake).run(AppendBlocks(page_id="pg", target_name="Идеи",
                                              page_title="Идеи", paragraphs=lines,
                                              markdown=True))
    sizes = [len(c[2]) for c in fake.calls if c[0] == "append_blocks"]
    assert sizes == [100, 100, 30]
    assert len(r.undo.block_ids) == 230


async def test_long_new_page_puts_the_rest_after_the_first_100(fake):
    await Executor(fake).run(CreatePage(parent_page_id="pg", target_name="Идеи", title="Т",
                                        body=[f"строка {i}" for i in range(150)], markdown=True))
    create = next(c for c in fake.calls if c[0] == "create_page")
    append = next(c for c in fake.calls if c[0] == "append_blocks")
    assert len(create[3]) == 100 and append[1] == "new-page" and len(append[2]) == 50


async def test_create_item_carries_a_markdown_body(fake):
    await Executor(fake).run(CreateItem(
        data_source_id="ds", target_name="TODO", markdown=True,
        properties=[pw("title", "Task", "title", "Скамейка")],
        body=["## Референсы", "![дуб](https://example.com/a.jpg)"]))
    children = next(c for c in fake.calls if c[0] == "create_page")[3]
    assert [b["type"] for b in children] == ["heading_2", "image"]


async def test_images_are_rehosted_and_unfetchable_ones_become_links(fake):
    images = FakeImages({"https://example.com/ok.jpg": "upload-1"})
    await Executor(fake, images=images).run(AppendBlocks(
        page_id="pg", target_name="Идеи", page_title="Идеи", markdown=True,
        paragraphs=["![хорошая](https://example.com/ok.jpg)",
                    "![битая](https://example.com/404.jpg)"]))
    ok, broken = fake.calls[-1][2]
    assert ok["image"] == {"type": "file_upload", "file_upload": {"id": "upload-1"},
                           "caption": [{"type": "text", "text": {"content": "хорошая"}}]}
    assert broken["type"] == "paragraph"
    link = broken["paragraph"]["rich_text"][0]["text"]
    assert link == {"content": "битая", "link": {"url": "https://example.com/404.jpg"}}


async def test_workspace_root_page_has_a_workspace_parent(fake):
    await Executor(fake).run(CreatePage(parent_page_id="workspace", target_name="Корень",
                                        title="Отпуск 2027"))
    assert fake.calls[-1][1] == {"type": "workspace", "workspace": True}


async def test_batch_undo_reverts_every_part_newest_first(fake):
    batch = UndoRecord(kind="batch", batch=[
        UndoRecord(kind="archive", page_id="p1"),
        UndoRecord(kind="delete_blocks", block_ids=["b1"]),
        UndoRecord(kind="archive", page_id="p2"),
    ])
    await Executor(fake).undo(UndoRecord.model_validate_json(batch.model_dump_json()))
    assert [c[:2] for c in fake.calls] == [("update_page", "p2"), ("delete_block", "b1"),
                                           ("update_page", "p1")]


def list_block(kind: str, text: str) -> dict:
    return {"id": f"b-{text}", "type": kind,
            kind: {"rich_text": [{"type": "text", "text": {"content": text}}]}}


async def test_a_short_line_joins_the_to_do_list_a_page_ends_with(fake):
    fake.page_blocks["pg"] = [{"id": "b0", "type": "heading_2", "heading_2": {}},
                              list_block("to_do", "Дюна"), list_block("to_do", "Оппенгеймер")]
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="медиа", page_title="медиа",
                                          paragraphs=["Uncharted"], markdown=True))
    [added] = fake.calls[-1][2]
    assert added["type"] == "to_do" and added["to_do"]["checked"] is False
    assert added["to_do"]["rich_text"][0]["text"]["content"] == "Uncharted"


async def test_a_bulleted_list_is_matched_too_and_a_page_without_one_is_left_alone(fake):
    fake.page_blocks["pg"] = [list_block("bulleted_list_item", "Дюна")]
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="медиа", page_title="медиа",
                                          paragraphs=["Uncharted"], markdown=True))
    assert fake.calls[-1][2][0]["type"] == "bulleted_list_item"

    fake.page_blocks["plain"] = [list_block("paragraph", "просто абзац")]
    await Executor(fake).run(AppendBlocks(page_id="plain", target_name="п", page_title="п",
                                          paragraphs=["Просто строка"], markdown=True))
    assert fake.calls[-1][2][0]["type"] == "paragraph"


async def test_a_list_that_is_not_at_the_end_or_a_long_note_is_not_matched(fake):
    fake.page_blocks["pg"] = [list_block("to_do", "Дюна"),
                              list_block("paragraph", "а это уже не список")]
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="p", page_title="p",
                                          paragraphs=["Uncharted"], markdown=True))
    assert fake.calls[-1][2][0]["type"] == "paragraph"

    fake.page_blocks["pg2"] = [list_block("to_do", "Дюна")]
    await Executor(fake).run(AppendBlocks(page_id="pg2", target_name="p", page_title="p",
                                          paragraphs=["## Заметка", "- раз", "- два", "- три"],
                                          markdown=True))
    assert [b["type"] for b in fake.calls[-1][2]][0] == "heading_2"


async def test_the_page_is_not_read_when_the_model_formatted_the_note_itself(fake):
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="p", page_title="p",
                                          paragraphs=["- [ ] Uncharted"], markdown=True))
    assert not any(c[0] == "block_children" for c in fake.calls)
    assert fake.calls[-1][2][0]["type"] == "to_do"


async def test_the_blank_line_notion_leaves_at_the_end_does_not_hide_the_list(fake):
    """The real медиа page: heading, an empty tick box, and Notion's trailing empty paragraph."""
    fake.page_blocks["pg"] = [
        {"id": "h", "type": "heading_1",
         "heading_1": {"rich_text": [{"type": "text", "text": {"content": "смотреть"}}]}},
        {"id": "t", "type": "to_do", "to_do": {"rich_text": [], "checked": False}},
        {"id": "p", "type": "paragraph", "paragraph": {"rich_text": []}},
    ]
    await Executor(fake).run(AppendBlocks(page_id="pg", target_name="медиа", page_title="медиа",
                                          paragraphs=["Uncharted"], markdown=True))
    assert fake.calls[-1][2][0]["type"] == "to_do"


async def test_a_film_asked_for_in_the_users_own_words_ends_up_as_a_tick_box(fake):
    """End to end from the answer production actually returned (intent "create", the page
    target «медиа», the title field filled, no body) to the block Notion is sent."""
    ctx, snap = ctx_and_snapshot()
    interp = make_interp("create", cand(ctx, "t5", fields={"t5.f1": val("Uncharted")}))
    best = SemanticValidator().validate(interp, ctx, snap).best
    fake.page_blocks["pg-ideas"] = [
        {"id": "h", "type": "heading_1",
         "heading_1": {"rich_text": [{"type": "text", "text": {"content": "смотреть"}}]}},
        {"id": "t", "type": "to_do", "to_do": {"rich_text": [], "checked": False}},
        {"id": "p", "type": "paragraph", "paragraph": {"rich_text": []}},
    ]

    cmd = build_command(best, "create", "Надо посмотреть фильм Uncharted")
    result = await Executor(fake).run(cmd)

    assert not any(c[0] == "create_page" for c in fake.calls)
    written = fake.calls[-1][2][0]
    assert written["type"] == "to_do"
    assert written["to_do"]["rich_text"][0]["text"]["content"] == "Uncharted"
    assert result.undo is not None and result.undo.kind == "delete_blocks"
