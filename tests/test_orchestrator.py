"""End-to-end conversation flows through the Orchestrator: one fake LLM, one fake Notion, and
the real discovery-free pipeline (context → schema → validator → policy → command → executor)
over a temp SQLite audit database. Each test is one flow from documentation/FLOWS.md and asserts
what the user sees (Reply text/buttons), what reached Notion, and what was audited.

Two invariants are asserted almost everywhere, via `closed_events`: every handled message writes
exactly one `events` row, and every row is closed with a decision and a duration — including the
error exits, where the orchestrator must reply rather than raise."""

from __future__ import annotations

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
from app.conversation.orchestrator import Orchestrator
from app.conversation.session import SessionStore
from app.llm.base import LLMInvalidOutput, LLMUnavailable
from app.llm.context import ContextBuilder
from app.notion.errors import NotionError, NotionUnavailable
from app.notion.snapshot import WorkspaceSnapshot
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from tests.fakes import FakeDiscovery, FakeLLM, FakeNotionProvider
from tests.helpers import amb, cand, make_interp, val
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

NOW = SAMPLE_NOW.astimezone(UTC)
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


def make_bot(tmp_path: Path, *, inbox: str | None = INBOX, inbox_mode: str = "auto") -> Bot:
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
    builder = ContextBuilder(settings.timezone, settings.items_per_target)
    orch = Orchestrator(
        settings, discovery, builder, llm, SemanticValidator(),
        Policy(Thresholds.from_settings(settings)), Executor(notion), store,
        SessionStore(store), clock=clock,
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
    """One events row per handled message, each closed with a decision and a duration."""
    evs = rows(bot, "events")
    for e in evs:
        assert e["decision"], f"event {e['id']} ({e['kind']}) was not closed with a decision"
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

    assert question.text == texts.QUESTION["target"]
    labels = [b.label for row in question.buttons for b in row]
    assert labels == ["Покупки", "Задачи", texts.BTN_CANCEL, texts.BTN_INBOX]
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
    bot.llm.queue(make_interp("search", cand(bot.ctx, "t2", 0.95, search_query="Rimi")))
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

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{reply.undo_id}")
    assert undone.text == texts.UNDONE
    assert notion_calls(bot, "delete_block") == [("delete_block", "blk-0")]
    assert rows(bot, "executions")[0]["undone"] == 1
    closed_events(bot, ["text", "callback"])


async def test_inbox_mode_button_does_not_save_on_a_rejection(make):
    bot = make(inbox_mode="button")
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    assert reply.text == texts.ERRORS["INTENT_UNKNOWN"]
    assert reply.buttons == []
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
    assert reply.undo_id is None
    assert notion_calls(bot, "append_blocks") == []


async def test_failed_inbox_write_degrades_to_a_message(bot):
    bot.notion.fail_append_blocks = NotionError(500, "server_error", "boom")
    bot.llm.queue(not_a_request(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "как дела?")

    assert reply.text == f"{texts.ERRORS['INTENT_UNKNOWN']} {texts.INBOX_FAILED}"
    assert reply.undo_id is None
    assert rows(bot, "executions") == []
    (event,) = closed_events(bot, ["text"])
    assert event["error"] == "INTENT_UNKNOWN"


async def test_page_inbox_target_records_one_execution_per_save(bot):
    bot.llm.queue(not_a_request(bot))
    await bot.orch.handle_text(CHAT, USER, "как дела?")
    execs = rows(bot, "executions")
    assert len(execs) == 1
    assert json.loads(execs[0]["undo"])["kind"] == "delete_blocks"


# ---- sessions -----------------------------------------------------------------------------------

async def test_expired_session_goes_to_the_inbox_and_the_new_message_is_handled(bot):
    bot.llm.queue(new_task(bot))
    await bot.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    bot.clock.advance(901)

    bot.llm.queue(buy_milk(bot))
    reply = await bot.orch.handle_text(CHAT, USER, "купи молоко в Рими")

    assert reply.text.startswith(texts.INBOX_SAVED_EXPIRED)
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
