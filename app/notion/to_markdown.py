"""Notion blocks → Obsidian markdown.

Pure and synchronous: the fetcher hands over a block tree whose `_children` are already filled
in, and three callbacks decide how to refer to things outside the page — another migrated page
(a [[link]] or nothing), a file (the name it will be saved under; the download happens later),
and an embedded database view (a query, or nothing). The reverse of app/notion/markdown.py."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

CHILDREN = "_children"
LIST_TYPES = ("bulleted_list_item", "numbered_list_item", "to_do")
FILE_TYPES = ("image", "video", "audio", "file", "pdf")
LINK_TYPES = ("bookmark", "link_preview", "embed")
SKIPPED = ("table_of_contents", "breadcrumb", "unsupported")
INDENT = "\t"


def _plain(rt: list[dict]) -> str:
    return "".join(r.get("plain_text", "") for r in rt)


def _wrap(text: str, mark: str) -> str:
    """`**text**`, with the whitespace the user left at either edge kept *outside* the marks:
    markdown does not read `** bold**` as bold."""
    core = text.strip()
    if not core:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()):]
    return f"{lead}{mark}{core}{mark}{trail}"


@dataclass
class Renderer:
    page_note: Callable[[str], str | None]
    file_name: Callable[[str, str], str]
    view: Callable[[dict], str] = lambda block: ""
    notes: list[str] = field(default_factory=list)  # what could not be carried over

    # ---- inline --------------------------------------------------------------------------

    def rich(self, rt: list[dict]) -> str:
        return "".join(self._run(r) for r in rt)

    def _page_ref(self, page_id: str | None, text: str) -> str | None:
        name = self.page_note(page_id) if page_id else None
        if name is None:
            return None
        return f"[[{name}]]" if not text or text == name else f"[[{name}|{text}]]"

    def _run(self, r: dict) -> str:
        text = r.get("plain_text", "")
        if r.get("type") == "mention":
            m = r.get("mention", {})
            if m.get("type") == "page":
                return self._page_ref(m["page"].get("id"), "") or text
            if m.get("type") == "link_mention":
                url = m["link_mention"].get("href", "")
                return f"[{text or url}]({url})" if url else text
            return text
        if r.get("type") == "equation":
            return f"${r.get('equation', {}).get('expression', text)}$"
        a = r.get("annotations", {})
        if a.get("code"):
            text = f"`{text}`"
        else:
            if a.get("bold"):
                text = _wrap(text, "**")
            if a.get("italic"):
                text = _wrap(text, "*")
            if a.get("strikethrough"):
                text = _wrap(text, "~~")
        href = r.get("href")
        if href:
            linked = self._page_ref(_notion_page_id(href), r.get("plain_text", ""))
            text = linked or (href if text == href else f"[{text}]({href})")
        return text

    # ---- blocks --------------------------------------------------------------------------

    def render(self, blocks: list[dict]) -> str:
        return "\n".join(self._blocks(blocks, 0)).strip("\n")

    def _blocks(self, blocks: list[dict], depth: int) -> list[str]:
        """Lines for a run of sibling blocks. A blank line separates blocks, except between
        items of one list — that would split it into several lists."""
        out: list[str] = []
        prev: str | None = None
        number = 0
        for b in blocks:
            kind = b.get("type", "")
            number = number + 1 if kind == "numbered_list_item" else 0
            lines = self._block(b, depth, number)
            if not lines:
                continue
            if out and not (kind in LIST_TYPES and prev in LIST_TYPES):
                out.append("")
            out += lines
            prev = kind
        return out

    def _children(self, b: dict, depth: int) -> list[str]:
        return self._blocks(b.get(CHILDREN, []), depth)

    def _block(self, b: dict, depth: int, number: int) -> list[str]:
        kind = b.get("type", "")
        data = b.get(kind, {}) or {}
        pad = INDENT * depth
        text = self.rich(data.get("rich_text", []))
        if kind == "paragraph":
            lines = [pad + text] if text else []
            return lines + self._nested(b, depth)
        if kind in ("heading_1", "heading_2", "heading_3"):
            lines = ["#" * int(kind[-1]) + " " + text]
            kids = self._children(b, depth)  # a toggle heading's contents follow it
            return lines + ([""] + kids if kids else [])
        if kind in LIST_TYPES:
            if kind == "to_do":
                mark = "- [x] " if data.get("checked") else "- [ ] "
            elif kind == "numbered_list_item":
                mark = f"{number}. "
            else:
                mark = "- "
            return [pad + mark + text] + self._children(b, depth + 1)
        if kind == "quote":
            return _quoted([text, *self._children(b, 0)])
        if kind == "callout":
            icon = (data.get("icon") or {}).get("emoji", "")
            return ["> [!note]", *_quoted([f"{icon} {text}".strip(), *self._children(b, 0)])]
        if kind == "toggle":
            return [f"> [!note]- {text}", *_quoted(self._children(b, 0))]
        if kind == "code":
            lang = data.get("language", "")
            lang = "" if lang == "plain text" else lang
            return [f"```{lang}", _plain(data.get("rich_text", [])), "```"]
        if kind == "divider":
            return ["---"]
        if kind == "equation":
            return ["$$", data.get("expression", ""), "$$"]
        if kind == "table":
            return self._table(b)
        if kind in FILE_TYPES:
            return self._file(kind, data)
        if kind in LINK_TYPES:
            url = data.get("url", "")
            caption = self.rich(data.get("caption", []))
            if not url:
                return []
            return [f"[{caption}]({url})" if caption else url]
        if kind == "child_page":
            name = self.page_note(b.get("id"))
            return [f"[[{name}]]" if name else data.get("title", "")]
        if kind == "link_to_page":
            target = data.get(data.get("type", ""), "")
            ref = self._page_ref(target, "")
            return [ref] if ref else []
        if kind == "child_database":
            view = self.view(b)
            return [view] if view else []
        if kind in ("column_list", "column", "synced_block"):
            return self._children(b, depth)
        if kind in SKIPPED:
            return []
        self.notes.append(kind)
        return [f"%% notion: {kind} %%"]

    def _nested(self, b: dict, depth: int) -> list[str]:
        """An indented block's children (a paragraph can have them in Notion)."""
        return self._children(b, depth + 1) if b.get(CHILDREN) else []

    def _table(self, b: dict) -> list[str]:
        rows = [[self.rich(cell).strip().replace("|", "\\|").replace("\n", " ")
                 for cell in r.get("table_row", {}).get("cells", [])]
                for r in b.get(CHILDREN, []) if r.get("type") == "table_row"]
        if not rows:
            return []
        width = max(len(r) for r in rows)
        rows = [r + [""] * (width - len(r)) for r in rows]
        lines = ["| " + " | ".join(rows[0]) + " |", "|" + " --- |" * width]
        return lines + ["| " + " | ".join(r) + " |" for r in rows[1:]]

    def _file(self, kind: str, data: dict) -> list[str]:
        source = data.get(data.get("type", ""), {}) or {}
        url = source.get("url", "")
        caption = self.rich(data.get("caption", []))
        if not url:
            return []
        name = self.file_name(url, kind)
        lines = [f"![[{name}]]"]
        return lines + ([f"*{caption}*"] if caption else [])


def _quoted(lines: list[str]) -> list[str]:
    return [f"> {line}" if line else ">" for line in lines]


def _notion_page_id(href: str) -> str | None:
    """The page id in a link to a Notion page ("/<32 hex>" or notion.so/...-<32 hex>)."""
    if "notion.so" not in href and not href.startswith("/"):
        return None
    tail = href.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
    raw = tail.rsplit("-", 1)[-1]
    if len(raw) != 32 or any(c not in "0123456789abcdef" for c in raw.lower()):
        return None
    raw = raw.lower()
    return f"{raw[:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:]}"


# ---- reading a page so it can be rewritten -----------------------------------------------

# Block types that are nothing but text, and so may be replaced when the user asks for a page
# to be rewritten. Everything else — an image, a file, a sub-page, an embedded database, a
# table, a column layout — is left exactly where it is, whatever the instruction says. This is
# a rule in the code and not a line in a prompt on purpose: a model that misreads a request to
# rewrite a page must not be able to delete someone's photographs.
REWRITABLE = frozenset({
    "paragraph", "heading_1", "heading_2", "heading_3",
    "bulleted_list_item", "numbered_list_item", "to_do", "quote", "code", "divider",
})
HEADING_TYPES = ("heading_1", "heading_2", "heading_3")


def is_rewritable(block: dict) -> bool:
    """A block with children is kept whatever its own type is: the children were never read,
    so there is no way to know that none of them is a picture."""
    return block.get("type") in REWRITABLE and not block.get("has_children")


def heading_text(block: dict) -> str:
    kind = block.get("type", "")
    if kind not in HEADING_TYPES:
        return ""
    return _plain(block.get(kind, {}).get("rich_text", []))


def section_of(blocks: list[dict], heading: str) -> tuple[int, int] | None:
    """Where the named section starts and ends: the heading itself and everything under it,
    down to the next heading of the same level or higher. None when no heading matches."""
    wanted = heading.strip().casefold()
    if not wanted:
        return None
    for i, block in enumerate(blocks):
        title = heading_text(block)
        if not title or title.strip().casefold() != wanted:
            continue
        level = int(block["type"][-1])
        for j in range(i + 1, len(blocks)):
            kind = blocks[j].get("type", "")
            if kind in HEADING_TYPES and int(kind[-1]) <= level:
                return i, j
        return i, len(blocks)
    return None


def page_markdown(blocks: list[dict]) -> str:
    """A page's blocks as plain markdown, for a model to read. Page links become their own
    text rather than [[wiki links]]: nothing here is going into a vault."""
    return Renderer(page_note=lambda _id: None,
                    file_name=lambda url, _kind: url).render(blocks)
