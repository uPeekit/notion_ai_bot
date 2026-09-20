"""Runs commands through the NotionProvider and produces undo records.

An UndoRecord is only produced when it can actually revert the write;
otherwise undo is None and the caller must not offer it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.commands.models import AppendBlocks, Command, CreateItem, CreatePage, Search, UpdateItem
from app.llm.sections import SectionPicker
from app.notion import props, titles
from app.notion.errors import NotionError
from app.notion.images import ImageHost
from app.notion.mapper import (
    content_blocks,
    create_item_payload,
    create_page_payload,
    properties_payload,
    read_to_write,
    search_filter,
)
from app.notion.markdown import rich_text
from app.notion.provider import NotionProvider

log = logging.getLogger(__name__)

SEARCH_LIMIT = 20
# Notion takes at most 100 blocks per create-page or append request; longer bodies (a web
# research result) go in batches.
MAX_BLOCKS_PER_REQUEST = 100
# A short plain append joins a list on the page; anything longer is a note of its own.
MAX_MATCHED_LINES = 3
LIST_BLOCKS = ("to_do", "bulleted_list_item", "numbered_list_item")
HEADINGS = ("heading_1", "heading_2", "heading_3")


def _title_of(cmd: CreateItem) -> str:
    value = next((p.value for p in cmd.properties if p.type == "title"), "")
    return value if isinstance(value, str) else ""


def _block_text(block: dict) -> str:
    kind = block.get("type", "")
    return "".join(r.get("plain_text", "") or r.get("text", {}).get("content", "")
                   for r in block.get(kind, {}).get("rich_text", []))


@dataclass(frozen=True)
class Section:
    """A heading and the blocks under it, down to the next heading. `tail` is the last block
    that says anything — Notion leaves an empty paragraph at the end of a page, and an empty
    tick box often sits where someone stopped typing; neither ends the list."""

    title: str
    tail: dict | None

    @property
    def list_kind(self) -> str | None:
        kind = (self.tail or {}).get("type")
        return kind if kind in LIST_BLOCKS else None


def _sections(blocks: list[dict]) -> list[Section]:
    """The page split at its headings. Blocks before the first heading are a section too (a
    page that is nothing but a list has exactly one)."""
    out: list[Section] = [Section("", None)]
    for b in blocks:
        if b.get("type") in HEADINGS:
            out.append(Section(_block_text(b), None))
        elif b.get("type") != "paragraph" or _block_text(b):
            out[-1] = Section(out[-1].title, b)
    return [s for s in out if s.tail is not None]


@dataclass(frozen=True)
class Written:
    name: str
    value: Any


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    page_id: str


class UndoRecord(BaseModel):
    kind: Literal["archive", "restore", "delete_blocks", "batch"]
    page_id: str | None = None
    properties: dict | None = None
    block_ids: list[str] = []
    partial: bool = False
    # kind "batch": every write of a multi-step plan, undone newest first ("undo all").
    batch: list[UndoRecord] = []


@dataclass
class ExecutionResult:
    command: Command
    page_id: str | None = None
    url: str | None = None
    block_ids: list[str] = field(default_factory=list)
    written: list[Written] = field(default_factory=list)
    undo: UndoRecord | None = None
    hits: list[SearchHit] = field(default_factory=list)
    # The row was already in the table, so nothing was written and there is nothing to undo;
    # page_id and url point at the row that is already there.
    existing: bool = False


class Executor:
    def __init__(self, provider: NotionProvider, images: ImageHost | None = None,
                 sections: SectionPicker | None = None) -> None:
        self._p = provider
        self._images = images
        self._sections = sections

    async def _duplicate(self, cmd: CreateItem) -> tuple[str, str] | None:
        """The row this create would duplicate, if the table already has that title."""
        title = _title_of(cmd)
        name = next((p.property_name for p in cmd.properties if p.type == "title"), "")
        if not title.strip():
            return None
        return await titles.existing(self._p, cmd.data_source_id, name, title)

    async def _prepare(self, blocks: list[dict]) -> list[dict]:
        """Image blocks re-hosted in Notion (see app.notion.images). One that cannot be fetched
        becomes a plain link rather than a broken image. Without an ImageHost they stay
        external."""
        if self._images is None:
            return blocks
        out = []
        for b in blocks:
            image = b.get("image") if b.get("type") == "image" else None
            if image is None or image.get("type") != "external":
                out.append(b)
                continue
            url = image["external"]["url"]
            upload_id = await self._images.host(url)
            if upload_id is None:
                caption = "".join(r["text"]["content"] for r in image.get("caption", []))
                line = f"[{caption or url}]({url})"
                out.append({"object": "block", "type": "paragraph",
                            "paragraph": {"rich_text": rich_text(line)}})
                continue
            hosted = {"type": "file_upload", "file_upload": {"id": upload_id}}
            if image.get("caption"):
                hosted["caption"] = image["caption"]
            out.append({"object": "block", "type": "image", "image": hosted})
        return out

    async def _match_list(
        self, page_id: str, blocks: list[dict], request: str = ""
    ) -> tuple[list[dict], str | None]:
        """A line added to a page joins the list it belongs in, and says which block to put it
        after. The bot cannot see a page's contents when it reads the message — only its
        sub-pages — so "add Uncharted to the films" would otherwise land as a stray paragraph
        at the very end, under whatever section happens to be last."""
        if not blocks or len(blocks) > MAX_MATCHED_LINES:
            return blocks, None
        if any(b.get("type") != "paragraph" for b in blocks):
            return blocks, None  # the model formatted it itself: leave it alone
        try:
            children = await self._p.block_children(page_id)
        except NotionError as e:
            log.info("could not read %s to match its list (%s)", page_id, e)
            return blocks, None
        lists = [s for s in _sections(children) if s.list_kind]
        if not lists:
            return blocks, None  # nothing on the page to join
        chosen = lists[-1]
        if len(lists) > 1:
            # Several lists, each under its own heading: only the model can say that a film
            # goes under "to watch" and not under "podcasts". Its answer is a choice between
            # headings the executor has already read — a short call, and only on this shape of
            # page. Without it (no request text, no picker, a failed call) the line goes to the
            # last list, which is what it did before.
            picked = await self._pick(request, blocks, [s.title for s in lists])
            chosen = lists[picked] if picked is not None else chosen
        kind = chosen.list_kind
        assert kind is not None
        extra = {"checked": False} if kind == "to_do" else {}
        joined = [{"object": "block", "type": kind,
                   kind: {"rich_text": b["paragraph"]["rich_text"], **extra}}
                  for b in blocks]
        return joined, (chosen.tail or {}).get("id")

    async def _pick(self, request: str, blocks: list[dict], titles: list[str]) -> int | None:
        if self._sections is None or not request.strip():
            return None
        line = " ".join(_block_text(b) for b in blocks).strip()
        return await self._sections.pick(request, line, titles)

    async def _append(
        self, block_id: str, blocks: list[dict], after: str | None = None
    ) -> list[str]:
        ids: list[str] = []
        for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
            data = await self._p.append_blocks(
                block_id, blocks[i:i + MAX_BLOCKS_PER_REQUEST], after)
            ids += [b["id"] for b in data.get("results", []) if "id" in b]
            if after and ids:
                after = ids[-1]  # the next batch follows the one just written, not the list
        return ids

    async def _create(self, parent: dict, properties: dict, blocks: list[dict]) -> dict:
        """Create a page with its body: the first 100 blocks in the create request itself, the
        rest appended to the new page."""
        blocks = await self._prepare(blocks)
        page = await self._p.create_page(parent, properties,
                                         blocks[:MAX_BLOCKS_PER_REQUEST] or None)
        if len(blocks) > MAX_BLOCKS_PER_REQUEST and page.get("id"):
            await self._append(page["id"], blocks[MAX_BLOCKS_PER_REQUEST:])
        return page

    async def run(self, cmd: Command) -> ExecutionResult:
        if isinstance(cmd, CreateItem):
            duplicate = await self._duplicate(cmd)
            if duplicate is not None:
                # Already in the table: writing it again is the one mistake a list of twenty
                # books makes easily. Its fields are left exactly as they are — a row that says
                # "Read" must not quietly become "To read" because a plan said so.
                page_id, url = duplicate
                return ExecutionResult(cmd, page_id, url, existing=True)
            parent, properties = create_item_payload(cmd)
            page = await self._create(parent, properties, content_blocks(cmd.body, cmd.markdown))
            page_id = page.get("id")
            # So the next step of the same plan sees it: a list of twenty can repeat itself.
            titles.remember(cmd.data_source_id, _title_of(cmd), page_id or "",
                            page.get("url") or "")
            return ExecutionResult(
                cmd,
                page_id,
                page.get("url"),
                written=[
                    Written(p.property_name, p.value)
                    for p in cmd.properties
                    if p.property_id in properties
                ],
                undo=UndoRecord(kind="archive", page_id=page_id) if page_id else None,
            )
        if isinstance(cmd, UpdateItem):
            before = await self._p.get_page(cmd.page_id)
            wanted = {p.property_id for p in cmd.properties}
            previous: dict = {}
            for prop in before.get("properties", {}).values():
                if prop.get("id") in wanted:
                    restore = read_to_write(prop)
                    if restore is not None:
                        previous[prop["id"]] = restore
            payload = properties_payload(cmd.properties)
            page = await self._p.update_page(cmd.page_id, properties=payload)
            return ExecutionResult(
                cmd,
                cmd.page_id,
                page.get("url") or before.get("url"),
                written=[
                    Written(p.property_name, p.value)
                    for p in cmd.properties
                    if p.property_id in payload
                ],
                undo=(
                    UndoRecord(
                        kind="restore",
                        page_id=cmd.page_id,
                        properties=previous,
                        partial=bool(set(payload) - previous.keys()),
                    )
                    if previous
                    else None
                ),
            )
        if isinstance(cmd, CreatePage):
            parent, properties, children = create_page_payload(cmd)
            page = await self._create(parent, properties, children)
            page_id = page.get("id")
            return ExecutionResult(
                cmd,
                page_id,
                page.get("url"),
                written=[Written("title", cmd.title)],
                undo=UndoRecord(kind="archive", page_id=page_id) if page_id else None,
            )
        if isinstance(cmd, AppendBlocks):
            blocks = await self._prepare(content_blocks(cmd.paragraphs, cmd.markdown))
            blocks, after = await self._match_list(cmd.page_id, blocks, cmd.request)
            ids = await self._append(cmd.page_id, blocks, after)
            return ExecutionResult(
                cmd,
                cmd.page_id,
                None,
                block_ids=ids,
                written=[Written("paragraphs", cmd.paragraphs)],
                undo=UndoRecord(kind="delete_blocks", block_ids=ids) if ids else None,
            )
        if isinstance(cmd, Search):
            return ExecutionResult(cmd, hits=await self._search(cmd))
        raise TypeError(f"unsupported command {type(cmd).__name__}")

    async def _search(self, cmd: Search) -> list[SearchHit]:
        if cmd.data_source_id:
            rows = await self._p.query_data_source(
                cmd.data_source_id, filter=search_filter(cmd), page_size=SEARCH_LIMIT
            )
        else:
            found = await self._p.search(cmd.query, "page")
            rows = [r for r in found if r.get("object") == "page"]
        return [
            SearchHit(props.page_title(r), r.get("url", ""), r["id"])
            for r in rows[:SEARCH_LIMIT]
        ]

    async def undo(self, rec: UndoRecord) -> None:
        if rec.kind == "batch":
            for part in reversed(rec.batch):
                await self.undo(part)
            return
        if rec.kind == "archive" and rec.page_id:
            await self._p.update_page(rec.page_id, archived=True)
        elif rec.kind == "restore" and rec.page_id:
            await self._p.update_page(rec.page_id, properties=rec.properties or {})
        elif rec.kind == "delete_blocks":
            for bid in rec.block_ids:
                # Undo may be pressed twice; a block already deleted must not abort the rest.
                try:
                    await self._p.delete_block(bid)
                except NotionError:
                    continue
