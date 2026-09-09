from app.notion.snapshot import WRITABLE_TYPES, FieldType

UNTITLED = "(без названия)"


def plain_text(rich: list[dict]) -> str:
    return "".join(r.get("plain_text", "") for r in rich)


def page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            text = plain_text(prop.get("title", [])).strip()
            return text or UNTITLED
    return UNTITLED


def field_type(prop: dict) -> FieldType:
    t = prop.get("type", "")
    return t if t in WRITABLE_TYPES else "readonly"  # type: ignore[return-value]


def item_hint(page: dict) -> str | None:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "status" and prop.get("status"):
            return prop["status"].get("name")
    for name, prop in props.items():
        if prop.get("type") == "checkbox" and prop.get("checkbox") is True:
            return name
    return None
