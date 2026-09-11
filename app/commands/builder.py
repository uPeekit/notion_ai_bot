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
from app.validation.semantic import MAX_TEXT, VCandidate, VField

SEARCH_FILTER_TYPES = frozenset({"select", "status", "multi_select", "relation", "checkbox"})


def paragraphs(text: str | None) -> list[str]:
    if not text:
        return []
    return [line.strip() for line in text.splitlines() if line.strip()]


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


def _title_value(c: VCandidate) -> str | None:
    f: VField | None = next((f for f in c.fields.values() if f.field.type == "title"), None)
    return f.value if f and f.status == "value" else None


def build_command(c: VCandidate, intent: str, raw_text: str) -> Command:
    t = c.target
    if intent == "create" and t.kind == "database":
        return CreateItem(data_source_id=t.id, target_name=t.name, properties=_writes(c))
    if intent == "create":
        return CreatePage(parent_page_id=t.id, target_name=t.name,
                          title=_title_value(c) or raw_text.strip()[:MAX_TEXT],
                          body=paragraphs(c.content))
    if intent == "update":
        assert c.item is not None
        return UpdateItem(page_id=c.item.id, target_name=t.name, item_title=c.item.title,
                          properties=_writes(c))
    if intent == "append":
        page = c.item
        return AppendBlocks(page_id=page.id if page else t.id, target_name=t.name,
                            page_title=page.title if page else t.name,
                            paragraphs=paragraphs(c.content))
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
            query = _title_value(c) or raw_text.strip()[:MAX_TEXT]
        return Search(data_source_id=t.id if t.kind == "database" else None, target_name=t.name,
                      title_property=title.name if title else None, query=query,
                      filters=filters)
    raise ValueError(f"unsupported intent {intent}")
