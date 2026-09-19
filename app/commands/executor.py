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
from app.notion import props
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
# A short plain append joins the list a page ends with; anything longer is a note of its own.
MAX_MATCHED_LINES = 3
LIST_BLOCKS = ("to_do", "bulleted_list_item", "numbered_list_item")


def _block_text(block: dict) -> str:
    kind = block.get("type", "")
    return "".join(r.get("plain_text", "") or r.get("text", {}).get("content", "")
                   for r in block.get(kind, {}).get("rich_text", []))


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


class Executor:
    def __init__(self, provider: NotionProvider, images: ImageHost | None = None) -> None:
        self._p = provider
        self._images = images

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

    async def _match_list(self, page_id: str, blocks: list[dict]) -> list[dict]:
        """A line added to a page that ends in a list joins that list. The bot cannot see a
        page's contents when it reads the message — only its sub-pages — so "add Uncharted to
        the films" would otherwise land as a stray paragraph under a list of tick boxes."""
        if not blocks or len(blocks) > MAX_MATCHED_LINES:
            return blocks
        if any(b.get("type") != "paragraph" for b in blocks):
            return blocks  # the model formatted it itself: leave it alone
        try:
            children = await self._p.block_children(page_id)
        except NotionError as e:
            log.info("could not read %s to match its list (%s)", page_id, e)
            return blocks
        # A Notion page almost always ends with an empty paragraph, and often with an empty
        # tick box someone left behind; neither says the list is over.
        meaningful = [b for b in children if b.get("type") != "paragraph" or _block_text(b)]
        last = meaningful[-1] if meaningful else None
        if last is None or last.get("type") not in LIST_BLOCKS:
            return blocks  # the page does not end in a list
        kind = last["type"]
        extra = {"checked": False} if kind == "to_do" else {}
        return [{"object": "block", "type": kind,
                 kind: {"rich_text": b["paragraph"]["rich_text"], **extra}}
                for b in blocks]

    async def _append(self, block_id: str, blocks: list[dict]) -> list[str]:
        ids: list[str] = []
        for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
            data = await self._p.append_blocks(block_id, blocks[i:i + MAX_BLOCKS_PER_REQUEST])
            ids += [b["id"] for b in data.get("results", []) if "id" in b]
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
            parent, properties = create_item_payload(cmd)
            page = await self._create(parent, properties, content_blocks(cmd.body, cmd.markdown))
            page_id = page.get("id")
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
            blocks = await self._match_list(cmd.page_id, blocks)
            ids = await self._append(cmd.page_id, blocks)
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
