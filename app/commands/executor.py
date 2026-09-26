"""Runs commands through the NotionProvider and produces undo records.

An UndoRecord is only produced when it can actually revert the write;
otherwise undo is None and the caller must not offer it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel

from app.commands.models import (
    AppendBlocks,
    Command,
    CreateItem,
    CreatePage,
    RewritePage,
    Search,
    UpdateItem,
)
from app.llm.edits import Edit, EditError, Editor, EditPlan, NothingToChange
from app.llm.edits import check as check_edits
from app.llm.rewrite import RewriteError, Rewriter
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
from app.notion.markdown import image_block, markdown_blocks, rich_text
from app.notion.provider import NotionProvider
from app.notion.to_markdown import (
    REWRITABLE,
    Line,
    is_rewritable,
    numbered,
    outline,
    page_markdown,
    strip_placeholders,
)
from app.vault.writer import VaultUndo

log = logging.getLogger(__name__)

SEARCH_LIMIT = 20
# How much of a page an edit may read. Long enough for anything a person writes by hand,
# short enough that one message cannot spend a minute paginating.
MAX_PAGE_BLOCKS = 300
# Notion takes at most 100 blocks per create-page or append request; longer bodies (a web
# research result) go in batches.
MAX_BLOCKS_PER_REQUEST = 100
# A short plain append joins a list on the page; anything longer is a note of its own.
MAX_MATCHED_LINES = 3
LIST_BLOCKS = ("to_do", "bulleted_list_item", "numbered_list_item")
HEADINGS = ("heading_1", "heading_2", "heading_3")
# What a short append may be made of and still be fitted to the page's own list. The model
# writes "- The Gentlemen" as readily as a bare line, and which of the two it chose says nothing
# about the page it lands on; anything richer (a heading, a quote, an image) is a note it
# meant to shape, and is left as written.
MATCHABLE = ("paragraph", *LIST_BLOCKS)


def _payload(kind: str, text: str) -> dict | None:
    """The block payload that puts `text` into a block of this same kind, or None when the
    new text is a different kind of block — Notion cannot change a block's type, so that
    has to become an insert and a delete instead."""
    blocks = markdown_blocks(text)
    if len(blocks) != 1 or blocks[0].get("type") != kind:
        return None
    return {kind: blocks[0][kind]}


def _title_of(cmd: CreateItem) -> str:
    value = next((p.value for p in cmd.properties if p.type == "title"), "")
    return value if isinstance(value, str) else ""


def _as(block: dict, kind: str) -> dict:
    """The same line as a block of the page's own list type. A tick box the model wrote keeps
    whether it was ticked; a bullet becoming a tick box starts unticked."""
    body: dict = {"rich_text": _rich_text(block)}
    if kind == "to_do":
        body["checked"] = bool(block.get("to_do", {}).get("checked", False))
    return {"object": "block", "type": kind, kind: body}


def _rich_text(block: dict) -> list:
    return block.get(block.get("type", ""), {}).get("rich_text", [])


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
    # "vault" is a turn that wrote only to the Obsidian vault: there is nothing for the Notion
    # executor to undo, and `vault` below holds the files to put back.
    kind: Literal["archive", "restore", "delete_blocks", "rewrite", "edits", "batch", "vault"]
    page_id: str | None = None
    properties: dict | None = None
    block_ids: list[str] = []
    partial: bool = False
    # kind "rewrite": the page's text before it was rewritten, as markdown lines. Undo
    # deletes `block_ids` and appends these again.
    markdown: list[str] = []
    # kind "edits": one entry per applied change, oldest first; undone newest first.
    edits: list[BlockEdit] = []
    # kind "batch": every write of a multi-step plan, undone newest first ("undo all").
    batch: list[UndoRecord] = []
    # Files the Obsidian side wrote in the same turn (app/vault/writer.py). The Notion executor
    # ignores these; the orchestrator hands them to the vault pipeline.
    vault: list[VaultUndo] = []


class BlockEdit(BaseModel):
    """One applied change, and everything needed to put it back.

    * `replaced` keeps the block's payload from before, which `update_block` takes as is.
    * `added` is deleted again.
    * `deleted` is restored from the trash — verified against the API: the same block id
      and the same picture come back, but at the *end* of the page, because Notion has no
      way to say where a restored block belongs.
    """

    kind: Literal["replaced", "added", "deleted"]
    block_id: str = ""
    body: dict = {}
    block_ids: list[str] = []


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
    # A rewrite's receipt: how big the text was, how big it is now, how many blocks were
    # left alone because they are not text, and the new text itself for the preview.
    before_lines: int = 0
    after_lines: int = 0
    kept_blocks: int = 0
    preview: str = ""
    # An edit script's receipt: how many places were changed, added, removed, and how many
    # of the removals were pictures or files (which the reply always names out loud).
    replaced: int = 0
    added: int = 0
    removed: int = 0
    removed_media: int = 0


class Refused(ValueError):
    """The command cannot be carried out as asked, for a reason the user should be told in
    plain words. `code` is a key of texts.ERRORS and `fmt` fills its placeholders."""

    def __init__(self, code: str, **fmt: Any) -> None:
        super().__init__(code)
        self.code = code
        self.fmt = fmt


class Executor:
    def __init__(self, provider: NotionProvider, images: ImageHost | None = None,
                 sections: SectionPicker | None = None,
                 rewriter: Rewriter | None = None,
                 editor: Editor | None = None) -> None:
        self._p = provider
        self._images = images
        self._sections = sections
        self._rewriter = rewriter
        self._editor = editor

    async def _duplicate(self, cmd: CreateItem) -> tuple[str, str] | None:
        """The row this create would duplicate, if the table already has that title."""
        title = _title_of(cmd)
        name = next((p.property_name for p in cmd.properties if p.type == "title"), "")
        if not title.strip():
            return None
        return await titles.existing(self._p, cmd.data_source_id, name, title)

    async def _prepare(self, blocks: list[dict], *,
                       link_fallback: bool = True) -> list[dict]:
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
            if upload_id is None and not link_fallback:
                # A web address the user typed themselves: keep the picture as it is rather
                # than quietly turning their instruction into a link. Re-hosting exists for
                # search results, whose links rot; this one was their own choice.
                out.append(b)
                continue
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
    ) -> tuple[list[dict], str | None, frozenset[str]]:
        """A line added to a page joins the list it belongs in, and says which block to put it
        after. The bot cannot see a page's contents when it reads the message — only its
        sub-pages — so "add Uncharted to the films" would otherwise land as a stray paragraph
        at the very end, under whatever section happens to be last."""
        if not blocks or len(blocks) > MAX_MATCHED_LINES:
            return blocks, None, frozenset()
        if any(b.get("type") not in MATCHABLE for b in blocks):
            # a note the model shaped on purpose: leave it alone
            return blocks, None, frozenset()
        try:
            children = await self._p.block_children(page_id)
        except NotionError as e:
            log.info("could not read %s to match its list (%s)", page_id, e)
            return blocks, None, frozenset()
        known = frozenset(b["id"] for b in children if b.get("id"))
        lists = [s for s in _sections(children) if s.list_kind]
        if not lists:
            return blocks, None, frozenset()  # nothing on the page to join
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
        return ([_as(b, kind) for b in blocks], (chosen.tail or {}).get("id"), known)

    async def _pick(self, request: str, blocks: list[dict], titles: list[str]) -> int | None:
        if self._sections is None or not request.strip():
            return None
        line = " ".join(_block_text(b) for b in blocks).strip()
        return await self._sections.pick(request, line, titles)

    async def _append(
        self, block_id: str, blocks: list[dict], after: str | None = None,
        known: frozenset[str] = frozenset(),
    ) -> list[str]:
        """The ids of the blocks this actually created.

        Notion answers an insert *after* a given block with the new blocks **and every
        sibling that follows them** (verified live: one paragraph inserted after the first
        of three comes back as three results). Those trailing ids are the user's own
        blocks, and treating them as ours would put them in an undo record — so Undo would
        delete lines nobody added. `known` is the ids the caller already read from the
        page; without it, only the first `len(batch)` results are trusted, because the new
        blocks come first."""
        ids: list[str] = []
        for i in range(0, len(blocks), MAX_BLOCKS_PER_REQUEST):
            batch = blocks[i:i + MAX_BLOCKS_PER_REQUEST]
            data = await self._p.append_blocks(block_id, batch, after)
            got = [b["id"] for b in data.get("results", []) if "id" in b]
            if after:
                seen = known | set(ids)
                fresh = ([b for b in got if b not in seen] if known
                         else got[:len(batch)])
            else:
                fresh = got  # an append to the end reports only what it added
            ids += fresh
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
            blocks, after, known = await self._match_list(cmd.page_id, blocks, cmd.request)
            ids = await self._append(cmd.page_id, blocks, after, known)
            return ExecutionResult(
                cmd,
                cmd.page_id,
                None,
                block_ids=ids,
                written=[Written("paragraphs", cmd.paragraphs)],
                undo=UndoRecord(kind="delete_blocks", block_ids=ids) if ids else None,
            )
        if isinstance(cmd, RewritePage):
            return await self._rewrite(cmd)
        if isinstance(cmd, Search):
            return ExecutionResult(cmd, hits=await self._search(cmd))
        raise TypeError(f"unsupported command {type(cmd).__name__}")

    async def _rewrite(self, cmd: RewritePage) -> ExecutionResult:
        """Change a page: either in places, or by replacing its text wholesale.

        The page is read once and shown to the model as numbered lines; the model answers with
        an edit script or with a whole new text, and picks which. Everything the model may do is
        decided here from what was actually read (`app/llm/edits.check`), never from the
        instruction: a line it invented, a sub-page it tried to delete, or a script that would
        empty the page cannot get through however the request was phrased."""
        children = await self._p.block_children(cmd.page_id, limit=MAX_PAGE_BLOCKS)
        lines = outline(children)
        if not any(line.editable or line.removable for line in lines):
            raise Refused("REWRITE_EMPTY", target_name=cmd.page_title)
        if self._editor is not None:
            plan, refused = await self._plan(cmd, lines)
            if refused:
                log.info("edits refused: %s", "; ".join(refused))
            if plan.edits:
                return await self._apply(cmd, plan.edits, lines, children)
            if plan.full.strip():
                return await self._replace_text(cmd, children, plan.full)
            if refused:
                raise Refused("REWRITE_FAILED", error=refused[0])
            raise Refused("REWRITE_NOTHING", target_name=cmd.page_title)
        # No editor configured: the whole-text rewriter is still a complete answer.
        return await self._rewrite_whole(cmd, children)

    async def _plan(self, cmd: RewritePage, lines: list[Line]) -> tuple[EditPlan, list[str]]:
        assert self._editor is not None
        try:
            plan, _, _ = await self._editor.plan(numbered(lines), cmd.instruction)
        except NothingToChange:
            raise Refused("REWRITE_NOTHING", target_name=cmd.page_title) from None
        except EditError as e:
            raise Refused("REWRITE_FAILED", error=str(e)) from None
        return check_edits(plan, lines)

    async def _apply(
        self, cmd: RewritePage, edits: list[Edit], lines: list[Line], children: list[dict],
    ) -> ExecutionResult:
        """Carry out an edit script against the blocks that were read.

        Order matters: text is changed in place first (no positions move), then insertions,
        then removals from the bottom up — so no edit is ever applied to a block that another
        edit has already moved or taken away."""
        by_n = {line.n: line for line in lines}
        blocks = {b.get("id"): b for b in children}
        on_page = frozenset(b["id"] for b in children if b.get("id"))
        done: list[BlockEdit] = []
        replaced = added = removed = removed_media = 0
        preview: list[str] = []

        for edit in [e for e in edits if e.op == "replace"]:
            line = by_n[edit.at]
            body = _payload(line.kind, edit.text)
            if body is None:
                # The new text is a different kind of block (a paragraph became a heading), and
                # Notion cannot change a block's type: put the new one in and take the old out.
                edits = [*edits, Edit(op="insert", at=edit.at, text=edit.text),
                         Edit(op="delete", at=edit.at)]
                continue
            before = blocks.get(line.id, {})
            await self._p.update_block(line.id, body)
            done.append(BlockEdit(kind="replaced", block_id=line.id,
                                  body={line.kind: before.get(line.kind, {})}))
            replaced += 1
            preview.append(edit.text)

        # Bottom-up, and in reverse for several insertions after one line, so they land in the
        # order the model wrote them.
        for edit in sorted([e for e in edits if e.op in ("insert", "image")],
                           key=lambda e: e.at, reverse=True):
            if edit.op == "image":
                new = await self._prepare([image_block(edit.url, edit.caption)],
                                          link_fallback=False)
            else:
                new = await self._prepare(markdown_blocks(edit.text))
            if not new:
                continue
            ids = await self._append(cmd.page_id, new, by_n[edit.at].id, on_page)
            if ids:
                done.append(BlockEdit(kind="added", block_ids=ids))
                added += len(ids)
                preview.append(edit.text or edit.url)

        for edit in sorted([e for e in edits if e.op == "delete"], key=lambda e: e.at,
                           reverse=True):
            line = by_n[edit.at]
            try:
                await self._p.delete_block(line.id)
            except NotionError as e:
                log.warning("could not remove %s: %s", line.id, e)
                continue
            done.append(BlockEdit(kind="deleted", block_ids=[line.id]))
            removed += 1
            removed_media += line.kind not in REWRITABLE

        if not done:
            raise Refused("REWRITE_FAILED", error="nothing applied")
        return ExecutionResult(
            cmd, cmd.page_id, None,
            written=[Written("rewrite", cmd.instruction)],
            replaced=replaced, added=added, removed=removed, removed_media=removed_media,
            preview="\n".join(preview),
            undo=UndoRecord(kind="edits", page_id=cmd.page_id, edits=done),
        )

    async def _rewrite_whole(self, cmd: RewritePage, children: list[dict]) -> ExecutionResult:
        if self._rewriter is None:
            raise Refused("REWRITE_UNAVAILABLE")
        current = page_markdown([b for b in children if is_rewritable(b)])
        if not current.strip():
            raise Refused("REWRITE_EMPTY", target_name=cmd.page_title)
        try:
            new_text, _, _ = await self._rewriter.rewrite(current, cmd.instruction)
        except RewriteError as e:
            raise Refused("REWRITE_FAILED", error=str(e)) from None
        return await self._replace_text(cmd, children, new_text)

    async def _replace_text(
        self, cmd: RewritePage, children: list[dict], new_text: str
    ) -> ExecutionResult:
        """Swap a page's text for a new whole text.

        Only blocks that are nothing but text and have no children of their own are archived: an
        image, a file, a sub-page, a database view or a nested list stays exactly where it is,
        whatever the instruction asked for. The new text is written before the old is archived,
        so a failure halfway leaves the page with too much rather than too little."""
        replaceable = [b for b in children if is_rewritable(b)]
        current = page_markdown(replaceable)
        if not replaceable:
            raise Refused("REWRITE_EMPTY", target_name=cmd.page_title)
        # The page was shown to the model with <...> placeholders where its pictures and
        # sub-pages are; a model that echoes one back must not write it as a line of text.
        lines = strip_placeholders(new_text).splitlines()
        blocks = await self._prepare(content_blocks(lines, True))
        if not blocks:
            raise Refused("REWRITE_FAILED", error="no blocks")
        # After the last kept block that comes before the text, so a page that opens with a
        # picture still opens with it. Nothing to anchor to means the end of the page.
        first = children.index(replaceable[0])
        kept_before = [b for b in children[:first] if b.get("id")]
        after = kept_before[-1]["id"] if kept_before else None
        ids = await self._append(cmd.page_id, blocks, after,
                                 frozenset(b["id"] for b in children if b.get("id")))
        for block in replaceable:
            bid = block.get("id")
            if not bid:
                continue
            try:
                await self._p.delete_block(bid)
            except NotionError as e:  # the new text is already there; saying nothing is worse
                log.warning("could not archive %s while rewriting: %s", bid, e)
        return ExecutionResult(
            cmd, cmd.page_id, None, block_ids=ids,
            written=[Written("rewrite", cmd.instruction)],
            before_lines=len(current.splitlines()), after_lines=len(lines),
            kept_blocks=len(children) - len(replaceable), preview=new_text,
            undo=UndoRecord(kind="rewrite", page_id=cmd.page_id, block_ids=ids,
                            markdown=current.splitlines()),
        )

    async def _unedit(self, part: BlockEdit) -> None:
        """One applied change, put back. A block that is already gone is skipped rather
        than failing the rest of the undo: the button can be pressed twice."""
        try:
            if part.kind == "replaced" and part.block_id:
                await self._p.update_block(part.block_id, part.body)
            elif part.kind == "added":
                for bid in part.block_ids:
                    await self._p.delete_block(bid)
            elif part.kind == "deleted":
                for bid in part.block_ids:
                    await self._p.restore_block(bid)
        except NotionError as e:
            log.warning("could not undo a %s edit: %s", part.kind, e)

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
        if rec.kind == "vault":  # nothing of this turn was written to Notion
            return
        if rec.kind == "batch":
            for part in reversed(rec.batch):
                await self.undo(part)
            return
        if rec.kind == "archive" and rec.page_id:
            await self._p.update_page(rec.page_id, archived=True)
        elif rec.kind == "restore" and rec.page_id:
            await self._p.update_page(rec.page_id, properties=rec.properties or {})
        elif rec.kind == "edits":
            # Newest first: a restored block comes back at the end of the page, so undoing
            # in reverse keeps what is left in the order it was in.
            for part in reversed(rec.edits):
                await self._unedit(part)
        elif rec.kind == "rewrite" and rec.page_id:
            # The new text goes, the old text comes back. Its block ids do not: Notion
            # gives new ones, and anything that linked to a single old block cannot be
            # restored. The words are what the user asked for back.
            for bid in rec.block_ids:
                try:
                    await self._p.delete_block(bid)
                except NotionError:
                    continue
            if rec.markdown:
                await self._append(rec.page_id,
                                   await self._prepare(content_blocks(rec.markdown, True)))
        elif rec.kind == "delete_blocks":
            for bid in rec.block_ids:
                # Undo may be pressed twice; a block already deleted must not abort the rest.
                try:
                    await self._p.delete_block(bid)
                except NotionError:
                    continue
