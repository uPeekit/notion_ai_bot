"""Tests for app/conversation/inbox.py: inbox_target() locates the one target the user
flagged as the inbox in a snapshot; inbox_command() turns arbitrary text into a
ready-to-execute Command against it. Does not import app.conversation.reply/session/resolver
(inbox.py is a leaf module) — Task 5's orchestrator is what decides when to call these."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from app.commands.models import AppendBlocks, CreateItem
from app.conversation.inbox import MAX_NOTE, MAX_TEXT, inbox_command, inbox_target
from app.notion.snapshot import WorkspaceSnapshot
from tools.sample_workspace import sample_snapshot

TZ = "Europe/Tallinn"
NOW = datetime(2026, 9, 11, 14, 30, tzinfo=UTC)


def _page():
    return sample_snapshot().target("pg-ideas")


def _db():
    return sample_snapshot().target("ds-buy")


def _flagged(target_id: str) -> WorkspaceSnapshot:
    snap = sample_snapshot()
    targets = [replace(t, is_inbox=True) if t.id == target_id else t for t in snap.targets]
    return WorkspaceSnapshot(fetched_at=snap.fetched_at, targets=targets)


def test_inbox_target_returns_none_when_nothing_flagged():
    assert inbox_target(sample_snapshot()) is None


def test_inbox_target_returns_the_flagged_target():
    snap = _flagged("pg-ideas")
    assert inbox_target(snap).id == "pg-ideas"


# ---- page target: AppendBlocks -----------------------------------------------------------

def test_page_target_one_paragraph_without_note():
    page = _page()
    cmd = inbox_command(page, "купить хлеб", note=None, now=NOW, tz=TZ)
    assert isinstance(cmd, AppendBlocks)
    assert cmd.page_id == page.id
    assert cmd.target_name == page.name
    assert cmd.page_title == page.name
    assert len(cmd.paragraphs) == 1
    assert cmd.paragraphs[0].endswith("купить хлеб")


def test_page_target_with_note_gives_two_paragraphs():
    page = _page()
    cmd = inbox_command(page, "текст сообщения", note="важно", now=NOW, tz=TZ)
    assert len(cmd.paragraphs) == 2
    assert cmd.paragraphs[1] == "важно"


def test_timestamp_uses_the_configured_timezone_not_machine_local():
    page = _page()
    cmd = inbox_command(page, "текст", note=None, now=NOW, tz=TZ)
    local = NOW.astimezone(ZoneInfo(TZ))
    assert cmd.paragraphs[0].startswith(f"{local:%Y-%m-%d %H:%M}")
    # sanity: Tallinn is UTC+3 in September, so the local hour differs from the UTC `now`
    assert local.hour != NOW.hour


def test_long_text_is_truncated_to_max_text():
    page = _page()
    cmd = inbox_command(page, "a" * 5000, note=None, now=NOW, tz=TZ)
    body = cmd.paragraphs[0].split(" — ", 1)[1]
    assert len(body) == MAX_TEXT


def test_long_note_is_truncated_to_max_note():
    page = _page()
    cmd = inbox_command(page, "текст", note="n" * 500, now=NOW, tz=TZ)
    assert len(cmd.paragraphs[1]) == MAX_NOTE


# ---- database target: CreateItem ---------------------------------------------------------

def test_database_target_creates_item_with_only_the_title_property():
    db = _db()
    cmd = inbox_command(db, "молоко", note=None, now=NOW, tz=TZ)
    assert isinstance(cmd, CreateItem)
    assert cmd.data_source_id == db.id
    assert len(cmd.properties) == 1
    prop = cmd.properties[0]
    assert prop.type == "title"
    assert prop.value == "молоко"


def test_database_without_title_property_raises_value_error():
    db = _db()
    no_title = replace(db, fields=[f for f in db.fields if f.type != "title"])
    with pytest.raises(ValueError):
        inbox_command(no_title, "молоко", note=None, now=NOW, tz=TZ)
