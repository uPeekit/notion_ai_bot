"""Command → Notion API JSON. Nothing else in the app builds Notion payloads."""

from __future__ import annotations

from typing import Any

from app.commands.models import CreateItem, CreatePage, PropertyWrite, Search

RICH_TEXT_LIMIT = 2000


def _rich(text: str) -> list[dict]:
    return [{"type": "text", "text": {"content": text[i:i + RICH_TEXT_LIMIT]}}
            for i in range(0, len(text), RICH_TEXT_LIMIT)] if text else []


def property_payload(p: PropertyWrite) -> dict | None:
    v: Any = p.value
    t = p.type
    try:
        if t in ("title", "rich_text"):
            return {t: _rich(v or "")}
        if t == "select":
            return {"select": {"id": v["id"]} if v else None}
        if t == "status":
            return {"status": {"id": v["id"]}} if v else None
        if t in ("multi_select", "relation"):
            return {t: [{"id": x["id"]} for x in (v or [])]}
        if t == "date":
            return {"date": {"start": v["start"], "end": v.get("end")} if v else None}
        if t == "checkbox":
            return {"checkbox": bool(v)}
        if t == "number":
            return {"number": v}
        if t == "url":
            return {"url": v}
        return None
    except (TypeError, KeyError):
        # A command rebuilt from the audit log may carry a malformed PropertyWrite (e.g. a select
        # whose value is a bare string); drop the property rather than crash the executor.
        return None


def properties_payload(props: list[PropertyWrite]) -> dict:
    out: dict = {}
    for p in props:
        payload = property_payload(p)
        if payload is not None:
            out[p.property_id] = payload
    return out


def paragraph_blocks(paragraphs: list[str]) -> list[dict]:
    return [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": _rich(p)}}
            for p in paragraphs if p]


def create_item_payload(cmd: CreateItem) -> tuple[dict, dict]:
    parent = {"type": "data_source_id", "data_source_id": cmd.data_source_id}
    return parent, properties_payload(cmd.properties)


def create_page_payload(cmd: CreatePage) -> tuple[dict, dict, list[dict]]:
    return ({"type": "page_id", "page_id": cmd.parent_page_id},
            {"title": {"title": _rich(cmd.title)}},
            paragraph_blocks(cmd.body))


def search_filter(cmd: Search) -> dict | None:
    """Notion data-source query filter, addressing properties by name. None means unfiltered
    (workspace search, or a data-source query with no narrowing conditions)."""
    if not cmd.data_source_id:
        return None
    conditions: list[dict] = []
    for f in cmd.filters:
        v = f.value
        if f.type in ("select", "status"):
            conditions.append({"property": f.property_name, f.type: {"equals": v["name"]}})
        elif f.type == "multi_select":
            conditions += [{"property": f.property_name, "multi_select": {"contains": x["name"]}}
                           for x in (v or [])]
        elif f.type == "relation":
            conditions += [{"property": f.property_name, "relation": {"contains": x["id"]}}
                           for x in (v or [])]
        elif f.type == "checkbox":
            conditions.append({"property": f.property_name, "checkbox": {"equals": bool(v)}})
    if cmd.query and cmd.title_property:
        conditions.append({"property": cmd.title_property, "title": {"contains": cmd.query}})
    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"and": conditions}


def read_to_write(prop: dict) -> dict | None:
    """Turn a property from a page *read* into the payload that restores it (for undo)."""
    if prop.get("has_more"):
        # Notion caps relation (and long rich_text) arrays on a page read and flags has_more;
        # restoring the truncated list would delete the rest, so drop it from `previous` instead
        # (the record becomes partial=True, an honest signal rather than silent data loss).
        return None
    t = prop.get("type")
    v = prop.get(t) if t else None
    if t in ("title", "rich_text"):
        # Restore keeps plain text only: undo does not restore bold/italic/links.
        return {t: _rich("".join(r.get("plain_text", "") for r in (v or [])))}
    if t == "select":
        return {"select": {"id": v["id"]} if v else None}
    if t == "status":
        return {"status": {"id": v["id"]}} if v else None
    if t in ("multi_select", "relation"):
        return {t: [{"id": x["id"]} for x in (v or [])]}
    if t == "date":
        return {"date": {"start": v["start"], "end": v.get("end")} if v else None}
    if t in ("checkbox", "number", "url"):
        return {t: v}
    return None
