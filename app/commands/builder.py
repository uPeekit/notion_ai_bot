"""VCandidate → Command. The only place that decides which fields get written."""

from __future__ import annotations

from app.commands.jsonvalue import to_json_value
from app.commands.models import (
    AppendBlocks,
    Command,
    CreateItem,
    CreatePage,
    PropertyWrite,
    Search,
    UpdateItem,
)
from app.llm.context import PAGE_TITLE_FIELD_ID
from app.llm.prompts import MAKE_WORDS, PAGE_WORDS
from app.validation.semantic import MAX_TEXT, VCandidate, VField

SEARCH_FILTER_TYPES = frozenset({"select", "status", "multi_select", "relation", "checkbox"})


def paragraphs(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


def markdown_lines(text: str | None) -> list[str]:
    """Text the model wrote, kept line for line (indentation inside a code fence matters) for
    the Markdown mapper; only surrounding blank lines go."""
    if not text or not text.strip():
        return []
    return [line.rstrip() for line in text.strip("\n").splitlines()]


def _writes(c: VCandidate) -> list[PropertyWrite]:
    out: list[PropertyWrite] = []
    for f in c.fields.values():
        if f.field.id == PAGE_TITLE_FIELD_ID or f.status not in ("value", "explicit_null"):
            continue
        out.append(PropertyWrite(property_id=f.field.id, property_name=f.field.name,
                                 type=f.field.type,
                                 value=to_json_value(f.value) if f.status == "value" else None))
    return out


def _search_filters(c: VCandidate) -> list[PropertyWrite]:
    return [
        PropertyWrite(property_id=f.field.id, property_name=f.field.name, type=f.field.type,
                      value=to_json_value(f.value))
        for f in c.fields.values()
        if f.status == "value" and f.field.type in SEARCH_FILTER_TYPES
    ]


def _one_line(raw_text: str) -> str:
    """The user's own words as a fallback page title or search query. They can be more than one
    line: a free-text answer is handed on as "<original request>
<answer>" (the orchestrator's
    MAX_PROMPT concatenation), and a Notion title holding a newline is not what either half of
    that meant. Collapses every run of whitespace, and truncates like every other free text."""
    return " ".join(raw_text.split())[:MAX_TEXT]


def _title_value(c: VCandidate) -> str | None:
    f: VField | None = next((f for f in c.fields.values() if f.field.type == "title"), None)
    return f.value if f and f.status == "value" else None


def asks_for_page(raw_text: str) -> bool:
    """Did the user ask for a page of its own? A make-word has to come before the page-word:
    "make a page" asks for one, "add to the page" only says where the line goes."""
    text = raw_text.lower()
    make = min((text.find(w) for w in MAKE_WORDS if w in text), default=-1)
    page = max((text.find(w) for w in PAGE_WORDS if w in text), default=-1)
    return make >= 0 and page > make


def build_command(c: VCandidate, intent: str, raw_text: str) -> Command:
    t = c.target
    if intent == "create" and t.kind == "database":
        return CreateItem(data_source_id=t.id, target_name=t.name, properties=_writes(c),
                          body=markdown_lines(c.content), markdown=True)
    if intent == "create":
        title = _title_value(c) or _one_line(raw_text)
        body = markdown_lines(c.content)
        if not body and not asks_for_page(raw_text):
            # A page target with a line and no body: the model answers "create" for "we should
            # watch the film X", but a sub-page with nothing in it is never what that meant —
            # the line belongs on the page, where the executor fits it to the list it lands in.
            return AppendBlocks(page_id=t.id, target_name=t.name, page_title=t.name,
                                paragraphs=[title], markdown=True)
        return CreatePage(parent_page_id=t.id, target_name=t.name, title=title,
                          body=body, markdown=True)
    if intent == "update":
        assert c.item is not None
        return UpdateItem(page_id=c.item.id, target_name=t.name, item_title=c.item.title,
                          properties=_writes(c))
    if intent == "append":
        page = c.item
        return AppendBlocks(page_id=page.id if page else t.id, target_name=t.name,
                            page_title=page.title if page else t.name,
                            paragraphs=markdown_lines(c.content), markdown=True)
    if intent == "search":
        title = t.title_field()
        filters = _search_filters(c)
        if c.search_query:
            query = c.search_query
        elif filters:
            # structured filters already narrow the search; don't also guess a free-text query
            # from the whole sentence.
            query = ""
        else:
            query = _title_value(c) or _one_line(raw_text)
        return Search(data_source_id=t.id if t.kind == "database" else None, target_name=t.name,
                      title_property=title.name if title else None, query=query,
                      filters=filters)
    raise ValueError(f"unsupported intent {intent}")
