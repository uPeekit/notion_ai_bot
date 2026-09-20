"""The per-message title index that stops a plan adding a row twice (app.notion.titles)."""

import pytest

from app.notion import titles
from app.notion.errors import NotionError
from tests.fakes import FakeNotionProvider


def row(page_id: str, title: str) -> dict:
    return {"id": page_id, "url": f"https://notion.so/{page_id}",
            "properties": {"Title": {"type": "title", "title": [{"plain_text": title}]}}}


@pytest.fixture
def fake():
    f = FakeNotionProvider()
    f.data_sources["ds-books"] = {"id": "ds-books"}
    f.data_sources["ds-big"] = {"id": "ds-big"}
    f.items["ds-books"] = [row("p1", "KGBT+"), row("p2", "Чапаев и Пустота")]
    return f


async def test_the_table_is_read_once_however_many_rows_are_checked(fake):
    with titles.collect():
        for title in ("KGBT+", "Чапаев и Пустота", "Омон Ра", "t"):
            await titles.existing(fake, "ds-books", "Title", title)

    assert len([c for c in fake.calls if c[0] == "query"]) == 1


async def test_case_and_spacing_do_not_make_it_a_different_row(fake):
    with titles.collect():
        assert await titles.existing(fake, "ds-books", "Title", "kgbt+") == (
            "p1", "https://notion.so/p1")
        assert await titles.existing(fake, "ds-books", "Title", "  ЧАПАЕВ   и пустота ")
        assert await titles.existing(fake, "ds-books", "Title", "Омон Ра") is None


async def test_a_row_written_during_the_message_is_not_written_again(fake):
    with titles.collect():
        assert await titles.existing(fake, "ds-books", "Title", "Омон Ра") is None
        titles.remember("ds-books", "Омон Ра", "p9", "https://notion.so/p9")
        assert await titles.existing(fake, "ds-books", "Title", "омон ра") == (
            "p9", "https://notion.so/p9")


async def test_nothing_is_remembered_between_messages(fake):
    with titles.collect():
        await titles.existing(fake, "ds-books", "Title", "KGBT+")
    with titles.collect():
        await titles.existing(fake, "ds-books", "Title", "KGBT+")

    assert len([c for c in fake.calls if c[0] == "query"]) == 2


async def test_a_table_that_cannot_be_read_refuses_nothing(fake):
    fake.fail_query = NotionError(403, "restricted", "no")
    with titles.collect():
        assert await titles.existing(fake, "ds-books", "Title", "KGBT+") is None


async def test_a_table_longer_than_the_index_is_asked_about_the_one_title(fake):
    fake.items["ds-big"] = [row(f"p{i}", f"Книга {i}")
                            for i in range(titles.MAX_INDEX_ROWS + 50)]
    with titles.collect():
        # Past the window that was read, so the table is asked about that one title instead.
        found = await titles.existing(fake, "ds-big", "Title", "книга 310")

    queries = [c for c in fake.calls if c[0] == "query"]
    assert found == ("p310", "https://notion.so/p310")
    assert len(queries) == 2 and queries[1][3] is not None  # the second one carries a filter
