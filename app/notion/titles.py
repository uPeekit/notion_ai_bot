"""What a database already holds, read once per message.

A plan that adds twenty books must not add a book that is already there, and neither the
planner nor the interpreter can be trusted to know: both are shown a capped list of rows
(ITEMS_PER_TARGET), so anything past that window looks new — which is how one workspace ended
up with both "KGBT+" and "kgbt+".

So the executor checks before it writes, against the table rather than the model's memory: the
first create into a database reads its titles once, and every later create of the same message
(a plan is one message) answers from that. Rows written along the way are added to the index,
so a plan cannot duplicate within itself either.

The index lives in a context variable for the duration of one message, which is also as long
as it can be trusted: nothing here is cached between messages.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from app.notion import props
from app.notion.errors import NotionError
from app.notion.provider import NotionProvider

log = logging.getLogger(__name__)

# Titles read per database. Three requests at Notion's 100 rows a page; a table longer than
# this is marked partial, and a create into it is checked with a query of its own instead.
MAX_INDEX_ROWS = 300
PAGE_SIZE = 100


def normal(title: str) -> str:
    """Two titles are the same row to a person: case and stray spacing do not count."""
    return " ".join(title.split()).casefold()


@dataclass
class Table:
    by_title: dict[str, tuple[str, str]] = field(default_factory=dict)  # title → (page id, url)
    partial: bool = False  # longer than MAX_INDEX_ROWS: absence here proves nothing

    def add(self, title: str, page_id: str = "", url: str = "") -> None:
        self.by_title.setdefault(normal(title), (page_id, url))


_tables: ContextVar[dict[str, Table] | None] = ContextVar("notion_titles", default=None)


@contextmanager
def collect() -> Iterator[None]:
    """One message's worth of index. Outside it nothing is remembered and nothing is read."""
    token = _tables.set({})
    try:
        yield
    finally:
        _tables.reset(token)


def remember(data_source_id: str, title: str, page_id: str = "", url: str = "") -> None:
    """A row this message just wrote, so the next step of the same plan sees it."""
    tables = _tables.get()
    if tables is not None and title.strip():
        tables.setdefault(data_source_id, Table()).add(title, page_id, url)


async def existing(
    provider: NotionProvider, data_source_id: str, title_property: str, title: str
) -> tuple[str, str] | None:
    """The row this database already has under that title — (page id, url) — or None.

    Reads the table once per message. A table too long to hold is queried for the one title
    instead, which is a filter Notion answers case-insensitively."""
    if not title.strip():
        return None
    tables = _tables.get()
    table = tables.get(data_source_id) if tables is not None else None
    if table is None:
        table = await _read(provider, data_source_id)
        if tables is not None:
            tables[data_source_id] = table
    hit = table.by_title.get(normal(title))
    if hit is not None:
        return hit
    if not table.partial:
        return None
    return await _query_one(provider, data_source_id, title_property, title)


async def _read(provider: NotionProvider, data_source_id: str) -> Table:
    table = Table()
    try:
        rows = await provider.query_data_source(data_source_id, page_size=MAX_INDEX_ROWS)
    except NotionError as e:
        log.info("could not read %s to check for duplicates (%s)", data_source_id, e)
        table.partial = True  # unknown, so no create is refused on the strength of it
        return table
    for row in rows[:MAX_INDEX_ROWS]:
        table.add(props.page_title(row), row.get("id", ""), row.get("url", ""))
    table.partial = len(rows) >= MAX_INDEX_ROWS
    return table


async def _query_one(
    provider: NotionProvider, data_source_id: str, title_property: str, title: str
) -> tuple[str, str] | None:
    if not title_property:
        return None
    try:
        rows = await provider.query_data_source(
            data_source_id,
            filter={"property": title_property, "title": {"contains": " ".join(title.split())}},
            page_size=PAGE_SIZE,
        )
    except NotionError as e:
        log.info("duplicate check for %r failed (%s)", title, e)
        return None
    wanted = normal(title)
    for row in rows:
        if normal(props.page_title(row)) == wanted:
            return row.get("id", ""), row.get("url", "")
    return None
