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
    if not cmd.data_source_id or not cmd.title_property:
        return None
    return {"property": cmd.title_property, "title": {"contains": cmd.query}}


def read_to_write(prop: dict) -> dict | None:
    """Turn a property from a page *read* into the payload that restores it (for undo)."""
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
