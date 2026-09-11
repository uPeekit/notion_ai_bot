"""Turns arbitrary text into a ready-to-execute Command against the one Notion target the
user flagged as the inbox — the safety net for a message the bot could not otherwise resolve
(the user checks it later and moves things themselves). Task 5's orchestrator decides *when*
to call this; this module only builds the Command.

inbox.py is a leaf: it may import app.texts, app.commands.* and app.notion.snapshot, but never
app.conversation.reply/session/resolver — those are callers, not dependencies, and importing
any of them here would be a cycle.

The inbox target stays an ordinary target for the LLM (app/llm/context.py does not special-case
it, and app/llm/prompts.py never mentions it): telling the model it's a fallback would teach it
to dump everything there instead of classifying, defeating the point of the classifier."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.commands.jsonvalue import to_json_value
from app.commands.models import AppendBlocks, Command, CreateItem, PropertyWrite
from app.notion.snapshot import Target, WorkspaceSnapshot

MAX_TEXT = 4000
MAX_NOTE = 200


def inbox_target(snapshot: WorkspaceSnapshot) -> Target | None:
    """The target flagged as the inbox, or None if the user hasn't flagged one. Discovery
    already resolved ties and the INBOX_TARGET_ID override (see
    app.notion.discovery.Discovery._resolve_inbox), so at most one target here has
    is_inbox=True."""
    return next((t for t in snapshot.targets if t.is_inbox), None)


def inbox_command(
    target: Target, text: str, *, note: str | None, now: datetime, tz: str
) -> Command:
    """Builds the Command that persists `text` (plus an optional short `note`) to the flagged
    inbox target: AppendBlocks for a page, CreateItem for a database. Never invents a target or
    a blank artifact — a database inbox with no title property, or text that is empty after
    stripping, raises ValueError; the caller (Task 5) catches it and degrades to a plain error
    reply rather than silently dropping the message or minting an empty Notion row."""
    body = text.strip()[:MAX_TEXT]
    if not body:
        raise ValueError("inbox text is empty")
    trimmed_note = note.strip()[:MAX_NOTE] if note else None

    if target.kind == "database":
        title = target.title_field()
        if title is None:
            raise ValueError(f"inbox target {target.id!r} has no title property")
        return CreateItem(
            data_source_id=target.id,
            target_name=target.name,
            properties=[
                PropertyWrite(
                    property_id=title.id, property_name=title.name, type=title.type,
                    value=to_json_value(body),
                )
            ],
        )

    local_now = now.astimezone(ZoneInfo(tz))
    paragraphs = [f"{local_now:%Y-%m-%d %H:%M} — {body}"]
    if trimmed_note:
        paragraphs.append(trimmed_note)
    return AppendBlocks(
        page_id=target.id, target_name=target.name, page_title=target.name,
        paragraphs=paragraphs,
    )
