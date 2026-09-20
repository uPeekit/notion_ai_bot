"""End-to-end conversation flows through the Orchestrator: one fake LLM, one fake Notion, and
the real discovery-free pipeline (context → schema → validator → policy → command → executor)
over a temp SQLite audit database. Each test is one flow from documentation/FLOWS.md and asserts
what the user sees (Reply text/buttons), what reached Notion, and what was audited.

Two invariants are asserted almost everywhere, via `closed_events`: every handled message writes
exactly one `events` row, and every row is closed with a decision and a duration — including the
error exits, where the orchestrator must reply rather than raise."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app import texts
from app.audit.store import AuditStore
from app.commands.executor import Executor
from app.config import Settings
from app.conversation.orchestrator import MAX_PROMPT, Orchestrator
from app.conversation.plan import StepField, StepSpec
from app.conversation.session import SessionStore
from app.llm.base import LLMInvalidOutput, LLMUnavailable
from app.llm.context import ContextBuilder
from app.notion.errors import NotionError, NotionUnavailable
from app.notion.snapshot import WorkspaceSnapshot
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from tests.fakes import FakeDiscovery, FakeLLM, FakeNotionProvider
from tests.helpers import amb, cand, make_interp, val
from tools.sample_workspace import sample_snapshot

# The audit store stamps events with the real wall clock, and the i:<event_id> payload's
# freshness check compares that stamp against the injected clock — so the test clock starts from
# the real now and moves forward with `Clock.advance`. Context keys do not depend on it.
NOW = datetime.now(UTC)
CHAT, USER = 42, 7
TOKEN = "ntn-test-token"

# Positional context keys over sample_snapshot(): t1 Дом (page), t2 Покупки, t3 Задачи,
# t4 Проекты, t5 Идеи (page). t2.f1 Название, t2.f2 Магазин, t2.f5 Куплено; t3.f1 Задача,
# t3.f2 Приоритет (required select A/B/C), t3.f3 Срок (date), t3.f4 Статус, t3.f7 Ссылка.
INBOX = "pg-ideas"


class Clock:
    def __init__(self, t: datetime) -> None:
        self.t = t

    def __call__(self) -> datetime:
        return self.t

    def advance(self, seconds: int) -> None:
        self.t += timedelta(seconds=seconds)


def flagged(target_id: str | None) -> WorkspaceSnapshot:
    snap = sample_snapshot()
    if target_id is None:
        return snap
    return WorkspaceSnapshot(
        snap.fetched_at, [replace(t, is_inbox=t.id == target_id) for t in snap.targets]
    )


@dataclass
class Bot:
    orch: Orchestrator
    llm: FakeLLM
    notion: FakeNotionProvider
    discovery: FakeDiscovery
    store: AuditStore
    sessions: SessionStore
    clock: Clock
    ctx: object
    snapshot: WorkspaceSnapshot
    db: Path


def make_bot(tmp_path: Path, *, inbox: str | None = INBOX, inbox_mode: str = "auto",
             researcher=None, planner=None) -> Bot:
    snap = flagged(inbox)
    db = tmp_path / "bot.sqlite"
    store = AuditStore(db)
    store.migrate()
    settings = Settings(
        notion_token=TOKEN, db_path=db, timezone="Europe/Tallinn", items_per_target=15,
        inbox_mode=inbox_mode, session_ttl_s=900, undo_window_s=300,
    )
    notion = FakeNotionProvider()
    llm = FakeLLM()
    discovery = FakeDiscovery(snap)
    clock = Clock(NOW)
    builder = ContextBuilder(settings.timezone, settings.items_per_target,
                             planning=planner is not None)
    orch = Orchestrator(
        settings, discovery, builder, llm, SemanticValidator(),
        Policy(Thresholds.from_settings(settings)), Executor(notion), store,
        SessionStore(store), clock=clock, researcher=researcher, planner=planner,
    )
    return Bot(orch, llm, notion, discovery, store, SessionStore(store), clock,
               builder.build(snap, now=NOW), snap, db)


@pytest.fixture
def make(tmp_path, env):
    built: list[Bot] = []

    def _make(**kw) -> Bot:
        b = make_bot(tmp_path, **kw)
        built.append(b)
        return b

    yield _make
    for b in built:
        b.store.close()


@pytest.fixture
def bot(make) -> Bot:
    return make()


# ---- shared assertions -----------------------------------------------------------------------

def rows(bot: Bot, table: str) -> list[dict]:
    con = sqlite3.connect(bot.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute(f"SELECT * FROM {table} ORDER BY rowid")]
    finally:
        con.close()


def closed_events(bot: Bot, kinds: list[str] | None = None) -> list[dict]:
    """One events row per handled message, each closed with a real decision and a duration. The
    orchestrator seeds the column with a placeholder when it opens the row, so "not null" proves
    nothing — what matters is that some exit replaced it."""
    evs = rows(bot, "events")
    for e in evs:
        decided = json.loads(e["decision"])["kind"]
        assert decided != "OPEN", f"event {e['id']} ({e['kind']}) was closed still unresolved"
        assert e["duration_ms"] is not None, f"event {e['id']} has no duration"
    if kinds is not None:
        assert [e["kind"] for e in evs] == kinds
    return evs


def token_of(reply) -> str:
    return reply.buttons[0][0].id.split(":")[1]


def press(reply, option_id: str) -> str:
    return f"a:{token_of(reply)}:{option_id}"


def button_ids(reply) -> list[str]:
    return [b.id for row in reply.buttons for b in row]


def notion_calls(bot: Bot, name: str) -> list[tuple]:
    return [c for c in bot.notion.calls if c[0] == name]


def page(page_id: str, title: str) -> dict:
    return {"id": page_id, "url": f"https://notion.so/{page_id}", "properties": {
        "Название": {"id": "title", "type": "title", "title": [{"plain_text": title}]}}}


# ---- F1: create, unambiguous -------------------------------------------------------------------

def buy_milk(bot: Bot):
    return make_interp("create", cand(bot.ctx, "t2", 0.95, fields={
        "t2.f1": val("Молоко", 1.0), "t2.f2": val("t2.f2.o1", 1.0)}))


async def test_f1_create_unambiguous_executes(bot):
    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert "Покупки" in reply.text and "Молоко" in reply.text
    assert "• Магазин: Rimi" in reply.text
    assert "https://notion.so/new-page" in reply.text
    execs = rows(bot, "executions")
    assert len(execs) == 1 and execs[0]["reply_message_id"] is None
    assert reply.undo_id == execs[0]["id"]
    assert button_ids(reply) == [f"u:{execs[0]['id']}"]
    parent = notion_calls(bot, "create_page")[0][1]
    assert parent == {"type": "data_source_id", "data_source_id": "ds-buy"}


async def test_f1_audits_the_whole_pipeline_without_secrets(bot):
    bot.llm.queue(buy_milk(bot))
    await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    (event,) = closed_events(bot, ["text"])
    assert event["raw_input"] == "купи молоко в Рими"
    assert event["llm_model"] == "fake-model"
    assert "Покупки" in event["llm_context"]
    assert event["llm_response"] and event["interpretation"]
    assert json.loads(event["decision"])["kind"] == "EXECUTE"
    assert event["executed"] == 1 and event["notion_page_id"] == "new-page"
    assert event["error"] is None
    assert TOKEN not in json.dumps(event, ensure_ascii=False)


async def test_voice_message_audits_the_transcript(bot):
    bot.llm.queue(buy_milk(bot))
    await bot.orch.handle_text(CHAT, USER, "купи молоко", kind="voice",
                               transcript="купи молоко")
    (event,) = closed_events(bot, ["voice"])
    assert event["transcription"] == "купи молоко"


# ---- F2: create, target ambiguous --------------------------------------------------------------

async def test_f2_target_ambiguous_then_button_executes(bot):
    bot.llm.queue(make_interp(
        "create",
        cand(bot.ctx, "t2", 0.88, fields={"t2.f1": val("Хлеб", 1.0)}),
        cand(bot.ctx, "t3", 0.82, fields={"t3.f1": val("Хлеб", 1.0),
                                          "t3.f2": val("t3.f2.o1", 1.0)}),
    ))
    question = await bot.orch.handle_text(CHAT, USER, "добавь хлеб")

    assert question.text == texts.QUESTION["target"].format(
        intent=texts.INTENT_LABELS["create"])
    labels = [b.label for row in question.buttons for b in row]
    assert labels == ["Покупки", "Задачи", texts.BTN_OTHER, texts.BTN_CANCEL, texts.BTN_INBOX]
    assert question.undo_id is None
    assert bot.sessions.get(CHAT, NOW) is not None

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "o0"))
    assert "Покупки" in reply.text and "Хлеб" in reply.text
    assert bot.llm.calls == 1  # a button answer never re-runs the LLM
    assert bot.sessions.get(CHAT, NOW) is None
    assert notion_calls(bot, "create_page")[0][1]["data_source_id"] == "ds-buy"
    closed_events(bot, ["text", "callback"])


# ---- F3: create, required field missing --------------------------------------------------------

def new_task(bot: Bot):
    return make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Подготовить документы", 1.0)}))


async def test_f3_required_field_missing_then_option_button(bot):
    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")

    assert "Приоритет" in question.text
    assert [b.label for row in question.buttons for b in row] == [
        "A", "B", "C", texts.BTN_CANCEL, texts.BTN_INBOX]

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "o0"))
    assert "• Приоритет: A" in reply.text
    properties = notion_calls(bot, "create_page")[0][2]
    assert properties["prio"] == {"select": {"id": "o-A"}}
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback"])


# ---- F4: free-text answer ----------------------------------------------------------------------

async def test_f4_free_text_answer_re_runs_the_llm_with_a_pending_block(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95)))  # no title -> ask for it
    question = await bot.orch.handle_text(CHAT, USER, "добавь в покупки")
    assert "Название" in question.text
    assert button_ids(question) == [press(question, "cancel"), press(question, "inbox")]

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "молоко")

    assert bot.llm.calls == 2
    text, payload, _schema = bot.llm.seen[1]
    assert text == "добавь в покупки\nмолоко"
    pending = payload["pending"]
    dumped = json.dumps(pending, ensure_ascii=False)
    assert "Название" in dumped and "Покупки" in dumped and "добавь в покупки" in dumped
    assert not re.search(r"t\d+(\.[fio]\d+)?", dumped), dumped
    for notion_id in ("ds-buy", "ds-todo", "pg-ideas", "b-milk", "o-Rimi", "prio"):
        assert notion_id not in dumped

    assert "Молоко" in reply.text
    assert bot.sessions.get(CHAT, NOW) is None
    closed_events(bot, ["text", "text"])


async def test_free_text_answer_carries_the_question_budget_forward(bot):
    """A button press spends one of the MAX_QUESTIONS answers; answering the *next* question in
    free text replaces the session, and the replacement must remember what was already asked."""
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Сделать отчёт", 1.0), "t3.f4": amb("t3.f4.o1", "t3.f4.o2")})))
    question = await bot.orch.handle_text(CHAT, USER, "сделать отчёт")
    answered = await bot.orch.handle_callback(CHAT, USER, press(question, "o0"))
    assert bot.sessions.get(CHAT, NOW).asked == ["field_required:prio"]
    assert "Статус" in answered.text

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Сделать отчёт", 1.0), "t3.f2": val("t3.f2.o1", 1.0),
        "t3.f4": amb("t3.f4.o1", "t3.f4.o2")})))
    await bot.orch.handle_text(CHAT, USER, "статус любой")

    replaced = bot.sessions.get(CHAT, NOW)
    assert replaced.asked == ["field_required:prio"]
    assert replaced.question.type == "field_ambiguous"
    assert replaced.original_text == "сделать отчёт\nстатус любой"


# ---- F5: unrelated message while a question is pending -----------------------------------------

async def test_f5_unrelated_message_replaces_the_session(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")

    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95,
                                             content="попробовать новый маршрут")))
    reply = await bot.orch.handle_text(CHAT, USER, "запиши идею: попробовать новый маршрут")

    assert "Идеи" in reply.text
    assert bot.sessions.get(CHAT, NOW) is None
    assert notion_calls(bot, "append_blocks")[0][1] == "pg-ideas"
    closed_events(bot, ["text", "text"])


# ---- F6/F7: update ------------------------------------------------------------------------------

def with_milk_page(bot: Bot) -> None:
    bot.notion.pages["b-milk"] = {
        "id": "b-milk", "url": "https://notion.so/b-milk",
        "properties": {"Куплено": {"id": "done", "type": "checkbox", "checkbox": False}},
    }


async def test_f6_update_by_reference(bot):
    with_milk_page(bot)
    bot.llm.queue(make_interp("update", cand(bot.ctx, "t2", 0.95, item="t2.i2",
                                             fields={"t2.f5": val(True, 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "отметь молоко купленным")

    call = notion_calls(bot, "update_page")[0]
    assert call[1] == "b-milk" and call[2] == {"done": {"checkbox": True}}
    assert "Молоко" in reply.text and "• Куплено: Да" in reply.text
    assert reply.undo_id is not None
    closed_events(bot, ["text"])


async def test_f7_item_ambiguous_button_updates_the_chosen_page(bot):
    bot.llm.queue(make_interp("update", cand(
        bot.ctx, "t2", 0.95, item_candidates=["t2.i2", "t2.i4"], item_text="молоко",
        fields={"t2.f5": val(True, 1.0)})))
    question = await bot.orch.handle_text(CHAT, USER, "отметь молоко купленным")
    assert [b.label for row in question.buttons for b in row] == [
        "Молоко", "Молоко овсяное", texts.BTN_CANCEL, texts.BTN_INBOX]

    await bot.orch.handle_callback(CHAT, USER, press(question, "o1"))
    assert notion_calls(bot, "update_page")[0][1] == "b-milk2"
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback"])


async def test_target_then_item_keeps_the_items_the_first_question_found(bot):
    """Two questions about one request. The item candidates belong to the session, not to the
    Decision that was thrown away with the first question: a rebuild that forgot them makes
    Policy fall through to item_not_found and offer to create a duplicate of a page that does
    exist ("Не нашёл ..." + [Добавить как новое])."""
    bot.llm.queue(make_interp(
        "update",
        cand(bot.ctx, "t2", 0.88, item_candidates=["t2.i2", "t2.i4"], item_text="молоко",
             fields={"t2.f5": val(True, 1.0)}),
        cand(bot.ctx, "t3", 0.82, fields={"t3.f4": val("t3.f4.o2", 1.0)}),
    ))
    target_q = await bot.orch.handle_text(CHAT, USER, "отметь молоко купленным")
    assert target_q.text == texts.QUESTION["target"].format(
        intent=texts.INTENT_LABELS["update"])

    item_q = await bot.orch.handle_callback(CHAT, USER, press(target_q, "o0"))
    assert item_q.text == texts.QUESTION_WITH_TARGET["item"].format(target_name="Покупки")
    assert [b.label for row in item_q.buttons for b in row] == [
        "Молоко", "Молоко овсяное", texts.BTN_CANCEL, texts.BTN_INBOX]

    reply = await bot.orch.handle_callback(CHAT, USER, press(item_q, "o1"))
    assert notion_calls(bot, "update_page")[0][1] == "b-milk2"
    assert "• Куплено: Да" in reply.text
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback", "callback"])


# ---- F8: item not found ------------------------------------------------------------------------

async def test_f8_item_not_found_add_new_creates_the_item(bot):
    bot.llm.queue(make_interp("update", cand(bot.ctx, "t2", 0.95, item_text="кефир",
                                             fields={"t2.f5": val(True, 1.0)})))
    question = await bot.orch.handle_text(CHAT, USER, "отметь кефир купленным")
    assert "кефир" in question.text
    assert [b.label for row in question.buttons for b in row] == [
        texts.BTN_ADD_NEW, texts.BTN_CANCEL, texts.BTN_INBOX]

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "add_new"))
    properties = notion_calls(bot, "create_page")[0][2]
    assert properties["title"]["title"][0]["text"]["content"] == "кефир"
    assert "кефир" in reply.text
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback"])


# ---- F9/F10/F11: append, sub-page, search -------------------------------------------------------

async def test_f9_append_reply_link_falls_back_to_the_target_url(bot):
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95,
                                             content="попробовать сыр с плесенью")))
    reply = await bot.orch.handle_text(CHAT, USER, "в идеи: попробовать сыр с плесенью")

    assert texts.DONE_LINK.format(url="https://notion.so/pg-ideas") in reply.text
    assert notion_calls(bot, "append_blocks")[0][1] == "pg-ideas"
    assert reply.undo_id is not None
    closed_events(bot, ["text"])


async def test_f10_create_sub_page(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t5", 0.95, content="план поездки",
                                             fields={"t5.f1": val("Отпуск 2027", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "создай страницу отпуск 2027 в идеях")

    call = notion_calls(bot, "create_page")[0]
    assert call[1] == {"type": "page_id", "page_id": "pg-ideas"}
    assert call[3] is not None  # the body paragraph
    assert texts.DONE_CREATE_PAGE.format(target_name="Идеи", item_title="Отпуск 2027") \
        in reply.text
    closed_events(bot, ["text"])


async def test_f11_search_formats_hits_without_an_execution_row(bot):
    bot.notion.data_sources["ds-buy"] = {"id": "ds-buy"}
    bot.notion.items["ds-buy"] = [page("b-bread", "Хлеб"), page("b-milk", "Молоко")]
    # The fake applies a title filter the way Notion does, so the query has to match a row.
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.95, search_query="Хлеб")))
    reply = await bot.orch.handle_text(CHAT, USER, "что у меня в покупках?")

    assert reply.text.startswith(texts.SEARCH_HEADER)
    assert "1. Хлеб — https://notion.so/b-bread" in reply.text
    assert reply.buttons == [] and reply.undo_id is None
    assert rows(bot, "executions") == []
    closed_events(bot, ["text"])


# ---- F12: low-confidence date ------------------------------------------------------------------

def report_with_shaky_date(bot: Bot):
    return make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Сделать отчёт", 1.0), "t3.f2": val("t3.f2.o1", 1.0),
        "t3.f3": val({"start": "2026-09-14"}, 0.5)}))


async def test_f12_low_confidence_date_confirm_executes(bot):
    bot.llm.queue(report_with_shaky_date(bot))
    question = await bot.orch.handle_text(CHAT, USER, "сделать отчёт к понедельнику")
    assert "14.09.2026" in question.text
    assert [b.label for row in question.buttons for b in row] == [
        texts.BTN_CONFIRM, texts.BTN_OTHER, texts.BTN_CANCEL, texts.BTN_INBOX]

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "confirm"))
    assert "• Срок: 14.09.2026" in reply.text
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback"])


async def test_f12_other_keeps_the_session_and_asks_for_free_text(bot):
    bot.llm.queue(report_with_shaky_date(bot))
    question = await bot.orch.handle_text(CHAT, USER, "сделать отчёт к понедельнику")

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "other"))
    assert reply.text == texts.ENTER_VALUE
    assert reply.buttons == []
    assert bot.sessions.get(CHAT, NOW) is not None
    assert bot.llm.calls == 1
    assert notion_calls(bot, "create_page") == []
    closed_events(bot, ["text", "callback"])


# ---- F14: infrastructure errors ----------------------------------------------------------------

async def test_llm_unavailable_replies_and_saves_to_the_inbox(bot):
    bot.llm.queue_error(LLMUnavailable("connection refused"))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко")

    assert reply.text.startswith(texts.ERRORS["LLM_UNAVAILABLE"])
    assert texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas") \
        in reply.text
    appended = notion_calls(bot, "append_blocks")[0]
    assert appended[1] == "pg-ideas"
    assert "купи молоко" in json.dumps(appended[2], ensure_ascii=False)
    assert reply.undo_id is not None
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "LLM_UNAVAILABLE"


async def test_llm_invalid_output_replies_and_saves_to_the_inbox(bot):
    bot.llm.queue_error(LLMInvalidOutput("not json", raw="{{"))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко")

    assert reply.text.startswith(texts.ERRORS["LLM_INVALID_OUTPUT"])
    assert notion_calls(bot, "append_blocks")
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "LLM_INVALID_OUTPUT"


async def test_notion_error_on_execute_replies_and_saves_to_the_inbox(bot):
    bot.notion.fail_create_page = NotionError(400, "validation_error", "body failed validation")
    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert reply.text.startswith(
        texts.ERRORS["NOTION_4XX"].format(message="body failed validation"))
    assert notion_calls(bot, "append_blocks")
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "NOTION_4XX" and event["executed"] == 0


async def test_notion_5xx_on_execute_uses_the_server_error_message(bot):
    bot.notion.fail_create_page = NotionError(502, "server_error", "bad gateway")
    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")
    assert reply.text.startswith(texts.ERRORS["NOTION_5XX"])


async def test_discovery_failure_replies_without_touching_the_inbox(bot):
    bot.discovery.fail = NotionUnavailable()
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко")

    assert reply.text == texts.ERRORS["DISCOVERY_FAILED"]
    assert reply.buttons == [] and reply.undo_id is None
    assert bot.notion.calls == []  # nothing to write to
    assert bot.llm.calls == 0
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "DISCOVERY_FAILED"


# ---- inbox fallback -----------------------------------------------------------------------------

def not_a_request(bot: Bot):
    return make_interp("unknown", cand(bot.ctx, "t2", 0.2), intent_conf=0.9)


async def test_rejected_message_is_appended_to_the_inbox_with_an_undo_button(bot):
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    assert reply.text.startswith(texts.ERRORS["INTENT_UNKNOWN"])
    assert "https://notion.so/pg-ideas" in reply.text
    assert notion_calls(bot, "append_blocks")[0][1] == "pg-ideas"
    assert reply.undo_id is not None
    # exactly the undo button: nothing was left unsaved, so there is nothing to offer
    assert button_ids(reply) == [f"u:{reply.undo_id}"]

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")
    assert undone.text == texts.UNDONE
    # two paragraphs: the message, then the note saying why it landed in the inbox
    assert notion_calls(bot, "delete_block") == [("delete_block", "blk-0"),
                                                 ("delete_block", "blk-1")]
    assert rows(bot, "executions")[0]["undone"] == 1
    closed_events(bot, ["text", "callback"])


async def test_inbox_mode_button_does_not_save_on_a_rejection_but_offers_it(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    assert reply.text == texts.ERRORS["INTENT_UNKNOWN"]
    assert notion_calls(bot, "append_blocks") == []
    (event,) = closed_events(bot, ["text"])
    assert button_ids(reply) == [f"i:{event['id']}"]
    assert [b.label for row in reply.buttons for b in row] == [texts.BTN_INBOX]


async def test_inbox_offer_saves_the_event_text_when_pressed(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")

    reply = await bot.orch.handle_callback(CHAT, USER, button_ids(rejected)[0])
    saved = texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas")
    assert saved in reply.text
    appended = notion_calls(bot, "append_blocks")[0]
    assert appended[1] == "pg-ideas"
    assert "как дела?" in json.dumps(appended[2], ensure_ascii=False)
    assert reply.undo_id is not None
    closed_events(bot, ["text", "callback"])


async def test_inbox_offer_of_another_chat_is_refused(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")

    reply = await bot.orch.handle_callback(CHAT + 1, USER, button_ids(rejected)[0])
    assert reply.text == texts.ERRORS["SESSION_EXPIRED"]
    assert notion_calls(bot, "append_blocks") == []


async def test_inbox_offer_expires_with_the_session_ttl(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")
    # well past SESSION_TTL_SECONDS: the events row is stamped by the store's own wall clock, so
    # the margin has to swallow however long the suite took to get here
    bot.clock.advance(2 * 900)

    reply = await bot.orch.handle_callback(CHAT, USER, button_ids(rejected)[0])
    assert reply.text == texts.ERRORS["SESSION_EXPIRED"]
    assert notion_calls(bot, "append_blocks") == []


async def test_inbox_offer_is_refused_when_the_mode_is_off(make):
    bot = make(inbox_mode="off")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")
    assert rejected.buttons == []

    (event,) = closed_events(bot, ["text"])
    reply = await bot.orch.handle_callback(CHAT, USER, f"i:{event['id']}")
    assert reply.text == texts.ERRORS["INTENT_UNKNOWN"]  # what the rejection itself said
    assert notion_calls(bot, "append_blocks") == []


async def test_inbox_mode_button_saves_when_the_question_button_is_pressed(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    assert press(question, "inbox") in button_ids(question)

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "inbox"))
    assert texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas") \
        in reply.text
    appended = notion_calls(bot, "append_blocks")[0]
    assert "добавь задачу подготовить документы" in json.dumps(appended[2], ensure_ascii=False)
    assert bot.sessions.get(CHAT, NOW) is None
    assert reply.undo_id is not None
    closed_events(bot, ["text", "callback"])


async def test_inbox_mode_off_rejects_plainly_and_offers_no_button(make):
    bot = make(inbox_mode="off")
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")
    assert reply.text == texts.ERRORS["INTENT_UNKNOWN"]
    assert notion_calls(bot, "append_blocks") == []

    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    assert press(question, "inbox") not in button_ids(question)
    assert press(question, "cancel") in button_ids(question)


async def test_no_flagged_inbox_target_rejects_plainly(make):
    bot = make(inbox=None)
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    assert reply.text == texts.ERRORS["INTENT_UNKNOWN"]
    assert reply.undo_id is None and reply.buttons == []  # nothing to offer
    assert notion_calls(bot, "append_blocks") == []


async def test_failed_inbox_write_degrades_to_a_message_and_offers_the_button(bot):
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    failed = texts.INBOX_FAILED.format(target_name="Идеи")
    assert reply.text == f"{texts.ERRORS['INTENT_UNKNOWN']} {failed}"
    assert reply.undo_id is None
    assert rows(bot, "executions") == []
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "INTENT_UNKNOWN"
    assert button_ids(reply) == [f"i:{event['id']}"]  # the user can still retry the save


async def test_page_inbox_target_records_one_execution_per_save(bot):
    bot.llm.queue(not_a_request(bot))
    await bot.orch.handle_text(CHAT, USER, "как дела?")
    execs = rows(bot, "executions")
    assert len(execs) == 1
    assert json.loads(execs[0]["undo"])["kind"] == "delete_blocks"


async def test_inbox_button_keeps_the_session_when_the_save_fails(bot):
    """Pressing [В разное] and being told it failed must not also destroy the text: the session
    is the only copy, and its own keyboard is what makes a second attempt possible."""
    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "inbox"))
    assert reply.text.startswith(texts.INBOX_FAILED.format(target_name="Идеи"))
    assert question.text in reply.text  # and the question it is still waiting on
    assert bot.sessions.get(CHAT, NOW) is not None
    assert press(question, "inbox") in button_ids(reply)  # pressable again, same token
    assert rows(bot, "executions") == []

    bot.notion.fail_append_blocks = None
    retried = await bot.orch.handle_callback(CHAT, USER, press(question, "inbox"))
    assert texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas") \
        in retried.text
    assert bot.sessions.get(CHAT, NOW) is None
    closed_events(bot, ["text", "callback", "callback"])


async def test_inbox_offer_fetches_a_snapshot_of_its_own(make):
    """A button press runs no discovery of its own, so the inbox must not depend on an earlier
    step having cached a snapshot — after a restart inside the session TTL there is none."""
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")
    bot.discovery.last = None  # as after a process restart

    reply = await bot.orch.handle_callback(CHAT, USER, button_ids(rejected)[0])
    assert texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas") \
        in reply.text
    assert notion_calls(bot, "append_blocks")[0][1] == "pg-ideas"


async def test_inbox_offer_pressed_twice_saves_once(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")
    offer = button_ids(rejected)[0]
    await bot.orch.handle_callback(CHAT, USER, offer)

    again = await bot.orch.handle_callback(CHAT, USER, offer)
    assert again.text == texts.INBOX_ALREADY_SAVED
    assert len(notion_calls(bot, "append_blocks")) == 1
    assert len(rows(bot, "executions")) == 1
    closed_events(bot, ["text", "callback", "callback"])


# ---- rejections the user can act on ------------------------------------------------------------

async def test_sem_type_rejection_names_the_field_it_could_not_parse(make):
    """A validator REJECT carries no candidate, so its message can only be filled from the
    Issue the validator recorded. Before that, every SEM_TYPE degraded to the generic
    "Не понял, что нужно сделать в Notion." — a message ERRORS.md never promised for it."""
    bot = make(inbox_mode="off")
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Сделать отчёт", 1.0), "t3.f3": val("not-a-date", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "сделать отчёт к не-дате")

    assert reply.text == texts.ERRORS["SEM_TYPE"].format(field_name="Срок")
    assert reply.text != texts.ERRORS["INTENT_UNKNOWN"]
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "SEM_TYPE"
    assert "SEM_TYPE" in json.loads(event["validation_result"])["issues"][0]["code"]


async def test_unsupported_op_rejection_names_the_target(make):
    bot = make(inbox_mode="off")
    # "Идеи" is a page: PAGE_OPERATIONS has create_page/append/search, never update.
    bot.llm.queue(make_interp("update", cand(bot.ctx, "t5", 0.95, item_text="что-то",
                                             fields={"t5.f1": val("x", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "измени идею")

    assert reply.text == texts.ERRORS["SEM_UNSUPPORTED_OP"].format(target_name="Идеи")
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "SEM_UNSUPPORTED_OP"


# ---- one chat at a time --------------------------------------------------------------------

def slow_append(bot: Bot):
    """Make the Notion write actually yield to the event loop, the way a real HTTP call does.
    Without a suspension point inside the guarded section the fakes are atomic by accident and
    a concurrency test proves nothing."""
    real = bot.notion.append_blocks

    async def append(block_id, children, after=None):
        await asyncio.sleep(0)
        return await real(block_id, children, after)

    bot.notion.append_blocks = append


async def test_two_presses_of_the_same_inbox_offer_save_once(make):
    """A double-tapped button reaches the bot as two callbacks ~100 ms apart, in two concurrent
    handlers. `executed` is read, a write is awaited, and only then is `executed` set — so
    without a per-chat lock both passes see executed=0 and the message is saved twice."""
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    rejected = await bot.orch.handle_text(CHAT, USER, "как дела?")
    offer = button_ids(rejected)[0]
    slow_append(bot)

    first, second = await asyncio.gather(
        bot.orch.handle_callback(CHAT, USER, offer),
        bot.orch.handle_callback(CHAT, USER, offer),
    )

    assert len(notion_calls(bot, "append_blocks")) == 1
    assert len(rows(bot, "executions")) == 1
    assert texts.INBOX_ALREADY_SAVED in (first.text, second.text)
    closed_events(bot, ["text", "callback", "callback"])


async def test_two_presses_of_the_same_undo_button_revert_once(make):
    bot = make()
    bot.llm.queue(buy_milk(bot))
    created = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")
    real_update = bot.notion.update_page

    async def update_page(page_id, *, properties=None, archived=None):
        await asyncio.sleep(0)
        return await real_update(page_id, properties=properties, archived=archived)

    bot.notion.update_page = update_page

    await asyncio.gather(
        bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}"),
        bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}"),
    )

    assert len(notion_calls(bot, "update_page")) == 1  # the second press found it undone
    assert rows(bot, "executions")[0]["undone"] == 1


async def test_two_chats_are_not_serialised_against_each_other(make):
    """The lock is per chat_id: one chat waiting on Notion must not hold up another."""
    bot = make()
    bot.llm.queue(buy_milk(bot))
    bot.llm.queue(buy_milk(bot))
    entered: list[int] = []
    release = asyncio.Event()

    real_create = bot.notion.create_page

    async def create_page(parent, properties, children=None):
        entered.append(len(entered))
        if len(entered) == 1:  # the first chat parks inside the guarded section
            await release.wait()
        return await real_create(parent, properties, children)

    bot.notion.create_page = create_page
    first = asyncio.create_task(bot.orch.handle_text(CHAT, USER, "купи молоко в Рими"))
    await asyncio.sleep(0)
    second = asyncio.create_task(bot.orch.handle_text(CHAT + 1, USER, "купи молоко в Рими"))
    await asyncio.wait_for(second, timeout=1)  # would deadlock behind one global lock

    release.set()
    await asyncio.wait_for(first, timeout=1)
    assert len(entered) == 2


async def test_the_lock_map_does_not_grow_with_the_number_of_chats(bot):
    for chat in range(5):
        await bot.orch.cancel(chat)
    assert bot.orch._locks == {} and bot.orch._waiting == {}


# ---- the inbox leaves a trace ------------------------------------------------------------------

def inbox_paragraphs(bot: Bot) -> list[str]:
    children = notion_calls(bot, "append_blocks")[0][2]
    return [rt["text"]["content"]
            for block in children
            for rt in block["paragraph"]["rich_text"]]


async def test_a_rescued_message_says_why_it_is_in_the_inbox(bot):
    bot.llm.queue_error(LLMUnavailable("connection refused"))
    await bot.orch.handle_text(CHAT, USER, "купи молоко")

    text, note = inbox_paragraphs(bot)
    assert "купи молоко" in text
    assert note == texts.INBOX_NOTE.format(reason=texts.ERRORS["LLM_UNAVAILABLE"])


async def test_an_unanswered_question_is_filed_with_the_question_itself(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)

    bot.llm.queue(buy_milk(bot))
    await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    text, note = inbox_paragraphs(bot)
    assert "добавь задачу подготовить документы" in text
    assert note.startswith("Остался без ответа вопрос:")
    assert "Приоритет" in note  # the question that was actually on screen


async def test_expired_rescue_that_fails_is_audited_on_the_turn(bot):
    """The only real blind spot the audit log had: the rescued message is lost, the row belongs
    to the *new* message and would otherwise close as a clean EXECUTE with error NULL."""
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")

    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert reply.text.startswith(texts.INBOX_FAILED.format(target_name="Идеи"))
    assert "Молоко" in reply.text  # the new message still went through
    events = closed_events(bot, ["text", "text"])
    assert events[1]["executed"] == 1
    assert events[1]["error"] == "INBOX_FAILED"


async def test_a_turns_own_error_outranks_the_expired_rescue_fallback(bot):
    """The fallback is a floor, not an override: a code the turn earns for itself is more
    informative and wins (the same rule _inbox_or_error already follows)."""
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")

    bot.llm.queue_error(LLMUnavailable("connection refused"))
    await bot.orch.handle_text(CHAT, USER, "купи молоко")

    events = closed_events(bot, ["text", "text"])
    assert events[1]["error"] == "LLM_UNAVAILABLE"


async def test_a_failed_inbox_button_resends_the_question_it_first_asked(bot):
    """The retry keyboard must carry the same words as the original question — dropping the
    target name turns "Какой элемент в «Покупки»?" into a bare "Какой элемент?"."""
    bot.llm.queue(make_interp("update", cand(
        bot.ctx, "t2", 0.95, item_candidates=["t2.i2", "t2.i4"], item_text="молоко",
        fields={"t2.f5": val(True, 1.0)})))
    question = await bot.orch.handle_text(CHAT, USER, "отметь молоко купленным")
    assert question.text == texts.QUESTION_WITH_TARGET["item"].format(target_name="Покупки")
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")

    retry = await bot.orch.handle_callback(CHAT, USER, press(question, "inbox"))
    assert retry.text.startswith(texts.INBOX_FAILED.format(target_name="Идеи"))
    assert question.text in retry.text
    assert button_ids(retry) == button_ids(question)  # the same answers, same token


# ---- free-text answers do not grow without bound -----------------------------------------------

async def test_repeated_free_text_answers_stop_growing_the_request(bot):
    """MAX_QUESTIONS only counts button answers, so free text can be answered forever; the
    concatenation the next prompt is built from has to stop somewhere short of an overflow —
    and it has to stop by dropping the *oldest* end. A cap that keeps the head instead freezes
    the conversation at the limit: the new answer is chopped off, the prompt is byte-identical
    to the previous round, and a deterministic model asks the same question forever."""
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95)))  # no title -> ask for it
    await bot.orch.handle_text(CHAT, USER, "добавь в покупки")
    for i in range(12):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95)))
        await bot.orch.handle_text(CHAT, USER, f"ответ {i} " + "ы" * 500)

    assert len(bot.sessions.get(CHAT, NOW).original_text) <= MAX_PROMPT
    prompts = [seen[0] for seen in bot.llm.seen]
    assert len(prompts[-1]) == MAX_PROMPT  # the cap really is being hit by now
    # the newest answer survives the trim, and each turn is a different request
    assert prompts[-1].endswith("ответ 11 " + "ы" * 500)
    assert prompts[-1] != prompts[-2]
    assert bot.sessions.get(CHAT, NOW).original_text.endswith("ы" * 500)


async def test_a_title_falling_back_to_the_request_stays_one_line(bot):
    """A create with no title uses the user's words, which after a free-text answer are two
    lines joined by a newline — not a Notion page title."""
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                             fields={"t3.f2": val("t3.f2.o1", 1.0)})))
    question = await bot.orch.handle_text(CHAT, USER, "создай страницу")
    assert question.buttons

    # an empty title is a value, so Policy does not ask for one and the builder falls back
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t5", 0.95,
                                             fields={"t5.f1": val("", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "про отпуск")

    title = notion_calls(bot, "create_page")[0][2]["title"]["title"][0]["text"]["content"]
    assert title == "создай страницу про отпуск"
    assert "\n" not in title


# ---- never raises to the transport -------------------------------------------------------------

async def test_unexpected_failure_becomes_an_internal_reply(bot):
    async def boom(*args, **kwargs):
        raise RuntimeError("kaput")

    bot.notion.create_page = boom  # not a NotionError: nothing in the pipeline expects it
    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert reply.text == texts.ERRORS["INTERNAL"]
    assert reply.buttons == []
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "INTERNAL"


async def test_an_audit_store_that_cannot_open_the_event_still_replies(bot):
    bot.store.close()  # every insert now raises: locked database, full disk, ...

    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко")
    assert reply.text == texts.ERRORS["INTERNAL"]
    assert rows(bot, "events") == []
    assert bot.llm.calls == 0


async def test_an_audit_store_that_cannot_close_the_event_still_replies(bot):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    bot.llm.queue(buy_milk(bot))
    bot.store.update_event = boom

    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")
    assert "Молоко" in reply.text  # the work is done and the answer is earned
    assert notion_calls(bot, "create_page")


async def test_flush_expired_sessions_never_raises_to_its_scheduler(bot):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("database is locked")

    bot.store.expired_sessions = boom
    assert await bot.orch.flush_expired_sessions() == 0


# ---- sessions -----------------------------------------------------------------------------------

async def test_expired_session_goes_to_the_inbox_and_the_new_message_is_handled(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)

    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert reply.text.startswith(texts.INBOX_SAVED_EXPIRED.format(target_name="Идеи"))
    assert "Молоко" in reply.text  # the new message was still handled
    appended = notion_calls(bot, "append_blocks")[0]
    assert "добавь задачу подготовить документы" in json.dumps(appended[2], ensure_ascii=False)
    assert bot.sessions.get(CHAT, bot.clock()) is None
    closed_events(bot, ["text", "text"])


async def test_flush_expired_sessions_saves_and_clears(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)

    assert await bot.orch.flush_expired_sessions() == 1
    appended = notion_calls(bot, "append_blocks")[0]
    assert "добавь задачу подготовить документы" in json.dumps(appended[2], ensure_ascii=False)
    assert rows(bot, "sessions") == []
    assert await bot.orch.flush_expired_sessions() == 0


async def test_stale_token_is_refused_without_applying_the_answer(bot):
    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")

    reply = await bot.orch.handle_callback(CHAT, USER, "a:deadbeef:o0")
    assert reply.text == texts.ERRORS["SESSION_EXPIRED"]
    assert notion_calls(bot, "create_page") == []
    # the real session survives an answer aimed at a stale token
    assert bot.sessions.get(CHAT, NOW).token == token_of(question)
    closed_events(bot, ["text", "callback"])


async def test_unknown_callback_prefix_is_refused(bot):
    reply = await bot.orch.handle_callback(CHAT, USER, "zz:1")
    assert reply.text == texts.ERRORS["SESSION_EXPIRED"]
    closed_events(bot, ["callback"])


async def test_callback_without_a_session_is_refused(bot):
    reply = await bot.orch.handle_callback(CHAT, USER, "a:abcd1234:o0")
    assert reply.text == texts.ERRORS["SESSION_EXPIRED"]


async def test_fourth_question_falls_back_to_the_inbox(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Сделать отчёт", 1.0),
        "t3.f4": amb("t3.f4.o1", "t3.f4.o2"),
        "t3.f3": val({"start": "2026-09-14"}, 0.5),
        "t3.f7": val("https://example.test", 0.5),
    })))
    reply = await bot.orch.handle_text(CHAT, USER, "сделать отчёт")
    for option_id in ("o0", "o0", "confirm"):  # приоритет, статус, срок
        assert reply.buttons, reply.text
        reply = await bot.orch.handle_callback(CHAT, USER, press(reply, option_id))


    assert texts.INBOX_SAVED.format(target_name="Идеи", url="https://notion.so/pg-ideas") \
        in reply.text
    appended = notion_calls(bot, "append_blocks")[0]
    assert "сделать отчёт" in json.dumps(appended[2], ensure_ascii=False)
    assert bot.sessions.get(CHAT, NOW) is None
    assert bot.llm.calls == 1
    closed_events(bot, ["text", "callback", "callback", "callback"])


async def test_cancel_button_drops_the_session(bot):
    bot.llm.queue(new_task(bot))
    question = await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "cancel"))
    assert reply.text == texts.CANCELLED
    assert bot.sessions.get(CHAT, NOW) is None
    assert notion_calls(bot, "create_page") == []
    closed_events(bot, ["text", "callback"])


async def test_cancel_command_drops_the_session(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")

    reply = await bot.orch.cancel(CHAT)
    assert reply.text == texts.CANCELLED
    assert bot.sessions.get(CHAT, NOW) is None
    closed_events(bot, ["text", "cancel"])


# ---- undo ----------------------------------------------------------------------------------------

async def test_undo_button_archives_the_page_and_marks_the_execution(bot):
    bot.llm.queue(buy_milk(bot))
    created = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")
    assert reply.text == texts.UNDONE
    assert notion_calls(bot, "update_page")[-1] == ("update_page", "new-page", None, True)
    assert rows(bot, "executions")[0]["undone"] == 1


async def test_second_undo_press_is_refused(bot):
    bot.llm.queue(buy_milk(bot))
    created = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")
    await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")

    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")
    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    assert len(notion_calls(bot, "update_page")) == 1
    closed_events(bot, ["text", "callback", "callback"])


async def test_undo_command_undoes_the_latest_execution(bot):
    bot.llm.queue(buy_milk(bot))
    await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    reply = await bot.orch.undo(CHAT)
    assert reply.text == texts.UNDONE
    closed_events(bot, ["text", "undo"])


async def test_undo_command_without_an_execution_is_refused(bot):
    reply = await bot.orch.undo(CHAT)
    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    closed_events(bot, ["undo"])


async def test_undo_after_the_window_is_refused(bot):
    bot.llm.queue(buy_milk(bot))
    created = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")
    bot.clock.advance(301)

    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")
    assert reply.text == texts.ERRORS["UNDO_EXPIRED"].format(minutes=5)
    assert notion_calls(bot, "update_page") == []


async def test_undo_failure_is_reported(bot):
    bot.llm.queue(buy_milk(bot))
    created = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    async def boom(page_id, *, properties=None, archived=None):
        raise NotionError(502, "server_error", "bad gateway")

    bot.notion.update_page = boom
    reply = await bot.orch.handle_callback(CHAT, USER, f"u:{created.undo_id}")
    assert reply.text == texts.ERRORS["UNDO_FAILED"].format(message="bad gateway")
    assert rows(bot, "executions")[0]["undone"] == 0
    closed_events(bot, ["text", "callback"])


# ---- guards --------------------------------------------------------------------------------------

def test_orchestrator_holds_no_russian_literals():
    root = Path(__file__).resolve().parents[1]
    source = (root / "app" / "conversation" / "orchestrator.py").read_text(encoding="utf-8")
    assert not re.search(r"[Ѐ-ӿ]", source), "every user string belongs in app/texts.py"


# ---- a target question is never a dead end ----------------------------------------------------


async def test_other_on_a_target_question_lets_the_user_say_where(bot):
    """The first real message, «надо забрать посылки», came back as one wrong option (Books)
    with nothing between accepting it and throwing the message away. [Другое] asks for a
    correction, which is re-read together with the original message."""
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.7, search_query="посылки")))
    question = await bot.orch.handle_text(CHAT, USER, "надо забрать посылки")
    assert texts.INTENT_LABELS["search"] in question.text

    prompt = await bot.orch.handle_callback(CHAT, USER, press(question, "other"))
    assert prompt.text == texts.ENTER_CORRECTION
    assert bot.llm.calls == 1  # the button itself asks nothing of the model
    assert bot.sessions.get(CHAT, NOW) is not None  # the question is still open

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("Забрать посылки", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))  # f2 required
    done = await bot.orch.handle_text(CHAT, USER, "это задача, в TODO")

    assert bot.llm.calls == 2
    text, _context = bot.llm.seen[-1][:2]
    assert "надо забрать посылки" in text and "это задача, в TODO" in text
    assert notion_calls(bot, "create_page"), done.text  # written to the corrected target
    assert done.undo_id is not None


# ---- cancel says what happened, and where the inbox would have been ----------------------------


async def test_cancel_without_an_inbox_says_where_to_set_one_up(make):
    bot = make(inbox=None)
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.7, search_query="посылки")))
    question = await bot.orch.handle_text(CHAT, USER, "надо забрать посылки")
    assert texts.BTN_INBOX not in [b.label for row in question.buttons for b in row]

    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "cancel"))

    assert reply.text.startswith(texts.CANCELLED)
    assert f"http://127.0.0.1:{bot.orch._s.admin_ui_port}" in reply.text


async def test_cancel_with_an_inbox_is_one_line(bot):
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.7, search_query="посылки")))
    question = await bot.orch.handle_text(CHAT, USER, "надо забрать посылки")
    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "cancel"))
    assert reply.text == texts.CANCELLED


async def test_cancel_with_the_inbox_switched_off_does_not_nag(make):
    bot = make(inbox=None, inbox_mode="off")
    reply = await bot.orch.cancel(CHAT)
    assert reply.text == texts.CANCELLED


# ---- the inbox gets the whole phrase, never the model's extraction ----------------------------


@pytest.mark.parametrize("inbox", [INBOX, "ds-todo"], ids=["page inbox", "database inbox"])
async def test_inbox_keeps_the_whole_message_not_what_the_model_extracted(make, inbox):
    """The model had pulled «посылки» out of «надо забрать посылки» (item_text and a search
    query). Whatever lands in the inbox is for a human to sort out later, so it must be the
    message exactly as sent. The user's own inbox is a database (TODO), so both kinds count."""
    bot = make(inbox=inbox)
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.7, search_query="посылки",
                                             item_text="посылки")))
    question = await bot.orch.handle_text(CHAT, USER, "надо забрать посылки")

    await bot.orch.handle_callback(CHAT, USER, press(question, "inbox"))

    written = json.dumps(bot.notion.calls, ensure_ascii=False)
    assert "надо забрать посылки" in written


# ---- web research -------------------------------------------------------------------------------


class FakeResearcher:
    def __init__(self, answer: str = "## Борщ\n- свёкла\n- капуста", fail: bool = False):
        self.answer, self.fail = answer, fail
        self.asked: list[tuple[str, str]] = []

    async def research(self, request: str, query: str, media: str = "text") -> str:
        from app.llm.research import ResearchError

        self.asked.append((request, query))
        self.media = media
        if self.fail:
            raise ResearchError("no answer")
        return self.answer


async def test_web_query_appends_what_the_research_found(make):
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))
    reply = await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши в идеи")

    assert researcher.asked == [("найди рецепт борща и запиши в идеи", "рецепт борща")]
    blocks = notion_calls(bot, "append_blocks")[0][2]
    assert [b["type"] for b in blocks] == ["heading_2", "bulleted_list_item",
                                           "bulleted_list_item"]
    assert reply.undo_id is not None  # the whole research result is one undoable append
    closed_events(bot, ["text"])


async def test_web_query_becomes_the_body_of_a_new_page(make):
    bot = make(researcher=FakeResearcher("## Референсы\n![дуб](https://example.com/a.jpg)"))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t5", 0.95, web_query="скамейка из дуба",
                                             fields={"t5.f1": val("Скамейка", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "найди референсы скамейки из дуба в идеи")

    children = notion_calls(bot, "create_page")[0][3]
    assert [b["type"] for b in children] == ["heading_2", "image"]


async def test_web_query_without_a_researcher_goes_to_the_inbox(bot):
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))
    reply = await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши в идеи")

    assert texts.ERRORS["WEB_UNAVAILABLE"] in reply.text
    [ev] = closed_events(bot, ["text"])
    assert ev["error"] == "WEB_UNAVAILABLE"


async def test_failed_research_replies_and_saves_the_message(make):
    bot = make(researcher=FakeResearcher(fail=True))
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))
    reply = await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши в идеи")

    assert texts.ERRORS["WEB_FAILED"] in reply.text
    # the inbox (Идеи) got the user's words, not a research result
    [call] = notion_calls(bot, "append_blocks")
    assert "найди рецепт борща" in call[2][0]["paragraph"]["rich_text"][0]["text"]["content"]


async def test_web_query_survives_a_target_question(make):
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    bot.llm.queue(make_interp(
        "append",
        cand(bot.ctx, "t5", 0.6, web_query="рецепт борща"),
        cand(bot.ctx, "t1", 0.55, web_query="рецепт борща"),
    ))
    question = await bot.orch.handle_text(CHAT, USER, "найди рецепт борща")
    assert researcher.asked == []  # nothing is looked up before the destination is settled

    await bot.orch.handle_callback(CHAT, USER, press(question, "o0"))
    assert researcher.asked == [("найди рецепт борща", "рецепт борща")]
    assert notion_calls(bot, "append_blocks")[0][2][0]["type"] == "heading_2"


async def test_the_models_own_question_is_asked_and_the_answer_re_read(bot):
    interp = make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="картинки к шагам"))
    bot.llm.queue(interp.model_copy(update={"clarify": "Найти картинки или не искать?"}))
    question = await bot.orch.handle_text(CHAT, USER, "не найди картинки к каждому шагу")

    assert question.text == "Найти картинки или не искать?"
    assert button_ids(question)[-2:] == [press(question, "cancel"), press(question, "inbox")]
    assert notion_calls(bot, "append_blocks") == []  # nothing written on a guess
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, content="без картинок")))
    await bot.orch.handle_text(CHAT, USER, "не надо картинок")
    assert "не найди картинки" in bot.llm.seen[-1][0] and "не надо" in bot.llm.seen[-1][0]
    assert notion_calls(bot, "append_blocks")


async def test_a_question_from_the_research_is_asked_instead_of_written(make):
    from app.llm.research import ResearchQuestion

    class Asking(FakeResearcher):
        async def research(self, request, query, media="text"):
            raise ResearchQuestion("Искать картинки или нет?")

    bot = make(researcher=Asking())
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="шаги борща")))
    reply = await bot.orch.handle_text(CHAT, USER, "не найди картинки к шагам борща")

    assert reply.text == "Искать картинки или нет?"
    assert notion_calls(bot, "append_blocks") == []
    assert bot.sessions.get(CHAT, NOW) is not None  # the answer continues this request


async def test_the_requested_media_reaches_the_researcher(make):
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    c = cand(bot.ctx, "t5", 0.95, web_query="тории")
    c["web_media"] = "text_and_images"
    bot.llm.queue(make_interp("append", c))
    await bot.orch.handle_text(CHAT, USER, "найди про тории с картинками в идеи")
    assert researcher.media == "text_and_images"


# ---- multi-step plans ---------------------------------------------------------------------------


def free(text: str) -> StepSpec:
    """A step the bot has to read with the model (the planner could not express it)."""
    return StepSpec(text=text)


class FakePlanner:
    """plan() returns the given steps; next() offers `then` in turn, then says done."""

    def __init__(self, steps, *, fail: bool = False, then: list[str] | None = None):
        self.model = "fake-planner"
        self.hints: list[str] = []
        self.steps = [free(s) if isinstance(s, str) else s for s in steps]
        self.fail, self.then = fail, list(then or [])
        self.checks: list[list[str]] = []

    async def plan(self, request, workspace, hint=""):
        from app.llm.planner import PlanError

        self.hints.append(hint)
        if self.fail:
            raise PlanError("empty plan")
        assert "Места в Notion" in workspace
        return "Всё разложено", list(self.steps)

    async def next(self, state, workspace):
        from app.llm.planner import Verdict

        self.checks.append([s.status for s in state.history])
        if self.then:
            return Verdict(done=False, summary="", next_step=self.then.pop(0))
        return Verdict(done=True, summary="сделано всё", next_step="")


def plan_interp(bot):
    return make_interp("plan", cand(bot.ctx, "t3", 0.9))


def collect(sent: list):
    async def progress(reply):
        sent.append(reply)
    return progress


async def test_a_plan_runs_every_step_reports_each_and_offers_undo_all(make):
    planner = FakePlanner(["добавь в покупки хлеб", "добавь в покупки молоко"])
    bot = make(planner=planner)
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Хлеб", 1.0)})))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    sent: list = []
    reply = await bot.orch.handle_text(CHAT, USER, "купи хлеб и молоко", progress=collect(sent))

    assert sent[0].text.startswith(texts.PLAN_HEADER.format(goal="Всё разложено"))
    assert "1. добавь в покупки хлеб" in sent[0].text
    assert sent[1].text.startswith("Шаг 1.") and "Хлеб" in sent[1].text
    assert sent[1].undo_id is not None  # each step keeps its own undo
    assert sent[2].text.startswith("Шаг 2.") and "Молоко" in sent[2].text
    assert reply.text == texts.PLAN_DONE.format(summary="сделано всё", done=2, total=2)
    assert [b.label for row in reply.buttons for b in row] == [texts.BTN_UNDO_ALL]
    assert planner.checks == [["done", "done"]]  # asked once, after the planned steps
    assert len(notion_calls(bot, "create_page")) == 2

    await bot.orch.handle_callback(CHAT, USER, reply.buttons[0][0].id)
    archived = [c for c in notion_calls(bot, "update_page") if c[3]]
    assert len(archived) == 2  # both rows undone by the one button
    closed_events(bot, ["text", "callback"])


async def test_a_step_that_asks_pauses_the_plan_and_the_answer_resumes_it(make):
    planner = FakePlanner(["добавь хлеб", "добавь в покупки молоко"])
    bot = make(planner=planner)
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp(  # step 1: shopping list or tasks?
        "create",
        cand(bot.ctx, "t2", 0.6, fields={"t2.f1": val("Хлеб", 1.0)}),
        cand(bot.ctx, "t3", 0.55, fields={"t3.f1": val("Хлеб", 1.0),
                                          "t3.f2": val("t3.f2.o1", 1.0)})))
    sent: list = []
    question = await bot.orch.handle_text(CHAT, USER, "хлеб и молоко", progress=collect(sent))
    assert question.text == texts.QUESTION["target"].format(
        intent=texts.INTENT_LABELS["create"])
    assert notion_calls(bot, "create_page") == [] and len(sent) == 1  # only the plan so far

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    later: list = []
    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "o0"),
                                           progress=collect(later))
    assert [c[1]["data_source_id"] for c in notion_calls(bot, "create_page")] == ["ds-buy"] * 2
    assert later[0].text.startswith("Шаг 1.") and later[1].text.startswith("Шаг 2.")
    assert reply.text.startswith("🏁")
    assert bot.sessions.get(CHAT, NOW) is None


async def test_a_failed_step_is_reported_not_filed_and_the_plan_goes_on(make):
    planner = FakePlanner(["расскажи анекдот", "добавь в покупки молоко"],
                          then=["добавь в покупки молоко"])
    bot = make(planner=planner)
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("unknown", cand(bot.ctx, "t2", 0.9)))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "план", progress=collect([]))

    assert planner.checks == [["failed"], ["failed", "done"]]  # after the failure, and at the end
    assert notion_calls(bot, "append_blocks") == []  # nothing went to the inbox
    assert reply.text == texts.PLAN_DONE.format(summary="сделано всё", done=1, total=2)


async def test_no_planner_means_no_plan(bot):
    bot.llm.queue(make_interp("plan", cand(bot.ctx, "t3", 0.9)))
    reply = await bot.orch.handle_text(CHAT, USER, "организуй переезд")
    assert reply.text == texts.ERRORS["PLAN_UNAVAILABLE"]


async def test_a_plan_that_cannot_be_made_goes_to_the_inbox(make):
    bot = make(planner=FakePlanner([], fail=True))
    bot.llm.queue(plan_interp(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "организуй переезд")
    assert texts.ERRORS["PLAN_FAILED"] in reply.text
    assert notion_calls(bot, "append_blocks")  # the message itself is kept


async def test_a_plan_stops_after_max_steps(make):
    from app.conversation.plan import MAX_STEPS

    steps = [f"добавь в покупки товар {i}" for i in range(MAX_STEPS + 5)]
    bot = make(planner=FakePlanner(steps))
    bot.llm.queue(plan_interp(bot))
    for i in range(MAX_STEPS):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                                 fields={"t2.f1": val(f"Товар {i}", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "длинный список", progress=collect([]))
    assert len(notion_calls(bot, "create_page")) == MAX_STEPS
    assert reply.text.startswith("⏹")


async def test_steps_are_never_offered_the_plan_intent(make):
    bot = make(planner=FakePlanner(["добавь в покупки хлеб"]))
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Хлеб", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "план", progress=collect([]))
    first, step = bot.llm.seen[0], bot.llm.seen[1]
    intent_enum = lambda schema: schema["properties"]["intent"]["properties"]["value"]["enum"]  # noqa: E731
    assert "plan" in intent_enum(first[2]) and "plan" not in intent_enum(step[2])
    assert step[1]["pending"]["plan"]["цель"] == "Всё разложено"


async def test_a_plan_with_a_side_question_still_starts(make):
    """Live: «Планируй поездку во Вьетнам…» came back as plan + clarify «укажи дату начала» and
    fell through to the validator, which raised KeyError('plan')."""
    bot = make(planner=FakePlanner(["добавь в покупки хлеб"]))
    bot.llm.queue(plan_interp(bot).model_copy(update={"clarify": "Укажи дату начала поездки"}))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Хлеб", 1.0)})))
    reply = await bot.orch.handle_text(CHAT, USER, "планируй поездку", progress=collect([]))
    assert reply.text.startswith("🏁")


async def test_a_plan_intent_that_reaches_the_validator_is_rejected_not_raised(bot):
    from app.validation.semantic import SemanticValidator

    result = SemanticValidator().validate(
        make_interp("plan", cand(bot.ctx, "t3", 0.9)), bot.ctx, bot.snapshot)
    assert result.rejected and result.issues[0].code == "INTENT_UNKNOWN"


async def test_a_null_ish_clarify_is_not_asked(bot):
    """Live: the model's clarify came back as '{"result":null}' and was shown as the question."""
    for junk in ('{"result":null}', "null", "  ", "???"):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95, fields={
            "t2.f1": val("Хлеб", 1.0)})).model_copy(update={"clarify": junk}))
        reply = await bot.orch.handle_text(CHAT, USER, "купи хлеб")
        assert "Хлеб" in reply.text, junk


async def test_an_empty_search_inside_a_plan_is_a_failed_step(make):
    planner = FakePlanner(["что у меня в покупках про слона", "добавь в покупки хлеб"],
                          then=["добавь в покупки хлеб"])
    bot = make(planner=planner)
    bot.notion.data_sources["ds-buy"] = {"id": "ds-buy"}
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.95, search_query="слон")))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Хлеб", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "план", progress=collect([]))
    assert planner.checks[0] == ["failed"]


async def test_a_long_question_is_cut_at_a_word_not_mid_word():
    from app.validation.semantic import MAX_CLARIFY, clean_clarify

    question = "Уточни: " + "достопримечательности " * 100
    cut = clean_clarify(question)
    assert len(cut) <= MAX_CLARIFY + 1 and cut.endswith("достопримечательности…")


async def test_answers_to_a_plan_question_reach_the_later_steps(make):
    planner = FakePlanner(["добавь хлеб", "добавь в покупки молоко"])
    bot = make(planner=planner)
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95, fields={
        "t2.f1": val("Хлеб", 1.0)})).model_copy(update={"clarify": "Сколько хлеба?"}))
    await bot.orch.handle_text(CHAT, USER, "хлеб и молоко", progress=collect([]))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Хлеб", 1.0)})))
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "два батона", progress=collect([]))
    step2_context = bot.llm.seen[-1][1]
    assert step2_context["pending"]["plan"]["ответы_пользователя"] == ["два батона"]


async def test_structured_steps_cost_no_llm_calls_and_no_checks_until_the_end(make):
    books = [StepSpec(text=f"добавь в покупки {name}", action="create", target="Покупки",
                      title=name, fields=[StepField(name="Магазин", value="Rimi")])
             for name in ("Хлеб", "Молоко", "Яйца")]
    planner = FakePlanner(books)
    bot = make(planner=planner)
    bot.llm.queue(plan_interp(bot))  # only the message itself is read by the model
    sent: list = []
    reply = await bot.orch.handle_text(CHAT, USER, "купи хлеб, молоко и яйца",
                                       progress=collect(sent))

    assert bot.llm.calls == 1  # no call per step
    assert planner.checks == [["done", "done", "done"]]  # one check, at the end
    created = notion_calls(bot, "create_page")
    assert len(created) == 3
    titles = [next(v["title"][0]["text"]["content"] for k, v in c[2].items() if k == "title")
              for c in created]
    assert titles == ["Хлеб", "Молоко", "Яйца"]
    assert [b.label for row in reply.buttons for b in row] == [texts.BTN_UNDO_ALL]
    assert sent[1].text.startswith("Шаг 1.")


async def test_a_step_the_planner_could_not_express_still_uses_the_model(make):
    steps = [StepSpec(text="добавь в покупки Хлеб", action="create", target="Покупки",
                      title="Хлеб"),
             free("отметь молоко купленным")]
    bot = make(planner=FakePlanner(steps))
    bot.llm.queue(plan_interp(bot))
    bot.llm.queue(make_interp("update", cand(bot.ctx, "t2", 0.95, item="t2.i2",
                                             fields={"t2.f5": val(True, 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "хлеб и молоко", progress=collect([]))

    assert bot.llm.calls == 2  # the message, and the one step the planner left as text
    assert bot.llm.seen[-1][0] == "отметь молоко купленным"
    assert notion_calls(bot, "update_page")


async def test_more_junk_clarifies_are_not_asked(bot):
    """Live, twice: the question came back as '{"result":null}' and as '-null'."""
    for junk in ('{"result":null}', "-null", "  null  ", "N/A", "нет", "?", "—", "\n", "null."):
        bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95, fields={
            "t2.f1": val("Хлеб", 1.0)})).model_copy(update={"clarify": junk}))
        reply = await bot.orch.handle_text(CHAT, USER, "купи хлеб")
        assert "Хлеб" in reply.text, junk
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95, fields={
        "t2.f1": val("Хлеб", 1.0)})).model_copy(update={"clarify": "Сколько хлеба?"}))
    asked = await bot.orch.handle_text(CHAT, USER, "купи хлеб")
    assert asked.text == "Сколько хлеба?"  # a real question still gets through


async def test_a_field_answered_once_is_not_asked_again_for_every_later_step(make):
    """«все книги Достоевского»: the first step stops to ask for the required field, and after
    the answer the rest of the plan takes the same value instead of asking per book."""
    tasks = [StepSpec(text=f"добавь в задачи {name}", action="create", target="Задачи",
                      title=name)
             for name in ("Купить билеты", "Собрать чемодан", "Сдать ключи")]
    bot = make(planner=FakePlanner(tasks))
    bot.llm.queue(plan_interp(bot))
    sent: list = []

    question = await bot.orch.handle_text(CHAT, USER, "собери поездку", progress=collect(sent))

    assert "Приоритет" in question.text
    assert notion_calls(bot, "create_page") == []  # nothing written while the question is open

    later: list = []
    reply = await bot.orch.handle_callback(CHAT, USER, press(question, "o0"),
                                           progress=collect(later))

    created = notion_calls(bot, "create_page")
    assert len(created) == 3  # all three written, only the first one asked
    assert all(c[2]["prio"] == {"select": {"id": "o-A"}} for c in created)
    assert reply.text.startswith("🏁")
    assert bot.sessions.get(CHAT, NOW) is None


async def test_the_planner_is_told_how_the_interpreter_read_the_message(make):
    """The interpreter has already read the message to decide it is a plan at all; its reading
    goes to the planner as a hint instead of being thrown away."""
    bot = make(planner=(planner := FakePlanner(["добавь в покупки молоко"])))
    interp = make_interp("plan", cand(bot.ctx, "t2", 0.9, web_query="романы Достоевского"))
    interp.notes = "это про книги, нужен список романов"
    interp.candidates[0].target_name = "Books"
    bot.llm.queue(interp)

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t2", 0.95,
                                             fields={"t2.f1": val("Молоко", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "все романы Достоевского", progress=collect([]))

    [hint] = planner.hints
    assert "это про книги" in hint
    assert "Books" in hint            # the target it settled on
    assert "романы Достоевского" in hint  # and what it wanted looked up


async def test_every_model_call_of_a_plan_is_in_the_audit_row(make):
    """One row per message, but a plan makes several calls: the row used to keep the first
    call's answer under the last call's model name."""
    steps = [StepSpec(text="добавь в покупки Хлеб", action="create", target="Покупки",
                      title="Хлеб"),
             StepSpec(text="добавь в покупки Молоко", action="create", target="Покупки",
                      title="Молоко")]
    bot = make(planner=FakePlanner(steps))
    bot.llm.queue(plan_interp(bot))

    await bot.orch.handle_text(CHAT, USER, "купи хлеб и молоко", progress=collect([]))

    [row] = rows(bot, "events")
    calls = json.loads(row["llm_response"])
    assert [c["kind"] for c in calls] == ["interpret", "plan", "step", "step", "check"]
    assert row["llm_model"] == "fake-model, fake-planner x2, plan-step x2"
    assert calls[1]["steps"] == 2 and calls[2]["step"] == "добавь в покупки Хлеб"


async def test_a_plan_whose_question_expires_says_so_instead_of_going_quiet(make):
    steps = [StepSpec(text="добавь в задачи Купить билеты", action="create", target="Задачи",
                      title="Купить билеты"),
             StepSpec(text="добавь в задачи Собрать чемодан", action="create", target="Задачи",
                      title="Собрать чемодан")]
    bot = make(planner=FakePlanner(steps))
    bot.llm.queue(plan_interp(bot))
    question = await bot.orch.handle_text(CHAT, USER, "собери поездку", progress=collect([]))
    assert question.buttons  # the plan is waiting on the required field

    bot.clock.advance(901)
    bot.llm.queue(make_interp("unknown", cand(bot.ctx, "t2", 0.9)))
    reply = await bot.orch.handle_text(CHAT, USER, "что там с планом?")

    assert texts.PLAN_ABANDONED.format(goal="Всё разложено", left=2) in reply.text


async def test_no_web_search_for_a_message_that_never_asked_for_one(make):
    """«Хочу посмотреть фильм Uncharted» is a line for a list. The model offers a web search
    for it anyway, which costs minutes of waiting and writes a whole page instead of a line."""
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t5", 0.95, web_query="фильм Uncharted",
                                             fields={"t5.f1": val("Uncharted", 1.0)})))

    reply = await bot.orch.handle_text(CHAT, USER, "Хочу посмотреть фильм Uncharted")

    assert researcher.asked == []                       # nothing was looked up
    assert notion_calls(bot, "create_page") == []       # and no page was made for it
    assert notion_calls(bot, "append_blocks") != []     # the line went onto the page
    assert reply.undo_id is not None


async def test_a_search_the_user_did_ask_for_still_happens(make):
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))

    await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши в идеи")

    assert researcher.asked != []


async def test_a_message_with_nothing_but_a_query_is_looked_up_even_unasked(make):
    """Dropping the query would leave nothing at all to write."""
    researcher = FakeResearcher()
    bot = make(researcher=researcher)
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))

    await bot.orch.handle_text(CHAT, USER, "рецепт борща в идеи")

    assert researcher.asked != []


async def test_the_undo_window_starts_when_the_write_happens(make):
    """A web search can spend minutes before anything is written; undo used to expire during
    it, and the button was already dead by the time the page appeared."""
    class SlowResearcher(FakeResearcher):
        async def research(self, request, query, media="text"):
            bot.clock.advance(240)  # four minutes of searching
            return await super().research(request, query, media)

    bot = make(researcher=SlowResearcher())
    bot.llm.queue(make_interp("append", cand(bot.ctx, "t5", 0.95, web_query="рецепт борща")))
    await bot.orch.handle_text(CHAT, USER, "найди рецепт борща и запиши в идеи")

    [row] = rows(bot, "executions")
    written_at = bot.clock.t
    assert datetime.fromisoformat(row["expires_at"]) > written_at + timedelta(seconds=290)
