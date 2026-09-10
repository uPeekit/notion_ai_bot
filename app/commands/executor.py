"""Runs commands through the NotionProvider and produces undo records.

An UndoRecord is only produced when it can actually revert the write;
otherwise undo is None and the caller must not offer it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.commands.models import AppendBlocks, Command, CreateItem, CreatePage, Search, UpdateItem
from app.notion import props
from app.notion.mapper import (
    create_item_payload,
    create_page_payload,
    paragraph_blocks,
    properties_payload,
    read_to_write,
    search_filter,
)
from app.notion.provider import NotionProvider

SEARCH_LIMIT = 20


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
    kind: Literal["archive", "restore", "delete_blocks"]
    page_id: str | None = None
    properties: dict | None = None
    block_ids: list[str] = []
    partial: bool = False


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
    def __init__(self, provider: NotionProvider) -> None:
        self._p = provider

    async def run(self, cmd: Command) -> ExecutionResult:
        if isinstance(cmd, CreateItem):
            parent, properties = create_item_payload(cmd)
            page = await self._p.create_page(parent, properties)
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
                undo=UndoRecord(
                    kind="restore",
                    page_id=cmd.page_id,
                    properties=previous,
                    partial=bool(wanted - previous.keys()),
                ),
            )
        if isinstance(cmd, CreatePage):
            parent, properties, children = create_page_payload(cmd)
            page = await self._p.create_page(parent, properties, children or None)
            page_id = page.get("id")
            return ExecutionResult(
                cmd,
                page_id,
                page.get("url"),
                written=[Written("title", cmd.title)],
                undo=UndoRecord(kind="archive", page_id=page_id) if page_id else None,
            )
        if isinstance(cmd, AppendBlocks):
            data = await self._p.append_blocks(cmd.page_id, paragraph_blocks(cmd.paragraphs))
            ids = [b["id"] for b in data.get("results", []) if "id" in b]
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
        if rec.kind == "archive" and rec.page_id:
            await self._p.update_page(rec.page_id, archived=True)
        elif rec.kind == "restore" and rec.page_id:
            await self._p.update_page(rec.page_id, properties=rec.properties or {})
        elif rec.kind == "delete_blocks":
            for bid in rec.block_ids:
                await self._p.delete_block(bid)
