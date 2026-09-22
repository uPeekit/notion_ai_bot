"""YAML frontmatter: Obsidian's note properties."""

from __future__ import annotations

from typing import Any

import yaml

FENCE = "---"


def render(props: dict[str, Any], body: str = "") -> str:
    """A note: its properties (None and empty values left out, order kept) and its body."""
    kept = {k: v for k, v in props.items() if v not in (None, "", [], {})}
    head = ""
    if kept:
        dumped = yaml.safe_dump(kept, allow_unicode=True, sort_keys=False,
                                default_flow_style=False, width=1000)
        head = f"{FENCE}\n{dumped}{FENCE}\n"
    body = body.strip("\n")
    if not body:
        return head
    return f"{head}\n{body}\n" if head else f"{body}\n"


def split(text: str) -> tuple[dict[str, Any], str]:
    """(properties, body). A note without a well-formed frontmatter block has no properties:
    its whole text is body, so nothing the user wrote is ever lost to a parse error."""
    if not text.startswith(FENCE + "\n") and not text.startswith(FENCE + "\r\n"):
        return {}, text
    lines = text.splitlines(keepends=True)
    for i in range(1, len(lines)):
        if lines[i].rstrip("\r\n") == FENCE:
            try:
                props = yaml.safe_load("".join(lines[1:i])) or {}
            except yaml.YAMLError:
                return {}, text
            if not isinstance(props, dict):
                return {}, text
            return props, "".join(lines[i + 1:]).lstrip("\r\n")
    return {}, text
