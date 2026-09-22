"""Both stores on one message: Notion as before, Obsidian next to it, and one Undo for both."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from app import texts
from app.audit.store import AuditStore
from app.commands.executor import Executor
from app.config import Settings
from app.conversation.orchestrator import Orchestrator
from app.conversation.session import SessionStore
from app.llm.context import ContextBuilder
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator
from app.vault.filer import Filer
from app.vault.index import VaultIndex
from app.vault.pipeline import VaultPipeline
from app.vault.writer import VaultWriter
from tests.fakes import FakeDiscovery, FakeLLM, FakeNotionProvider
from tests.helpers import cand, make_interp, val
from tests.test_orchestrator import CHAT, NOW, TOKEN, USER, Clock, flagged
from tests.test_vault_filer import FakeAnthropic

VAULT_NOW = datetime(2026, 9, 22, 18, 30)


@pytest.fixture
def bot(tmp_path, env):
    vault_dir = tmp_path / "vault"
    (vault_dir / texts.VAULT_AREAS_DIR).mkdir(parents=True)
    (vault_dir / f"{texts.VAULT_AREAS_DIR}/дом.md").write_text("---\ntag: home\n---\n",
                                                                encoding="utf-8")
    (vault_dir / f"{texts.VAULT_TASKS_NOTE}.md").write_text("## дом\n\n- [ ] счета #home\n",
                                                             encoding="utf-8")
    snap = flagged("t3")
    db = tmp_path / "bot.sqlite"
    store = AuditStore(db)
    store.migrate()
    settings = Settings(notion_token=TOKEN, db_path=db, timezone="Europe/Tallinn",
                        items_per_target=15, session_ttl_s=900, undo_window_s=300)
    notion, llm = FakeNotionProvider(), FakeLLM()
    index = VaultIndex(vault_dir)
    index.refresh()
    writer = VaultWriter(index, now=lambda: VAULT_NOW)
    claude = FakeAnthropic()
    vault = VaultPipeline(index, writer, Filer("", "claude-haiku-4-5", client=claude),
                          None, now=lambda: VAULT_NOW)
    builder = ContextBuilder(settings.timezone, settings.items_per_target)
    orch = Orchestrator(settings, FakeDiscovery(snap), builder, llm, SemanticValidator(),
                        Policy(Thresholds.from_settings(settings)), Executor(notion), store,
                        SessionStore(store), clock=Clock(NOW), vault=vault)
    yield type("VaultBot", (), {
        "orch": orch, "llm": llm, "claude": claude, "index": index, "store": store,
        "notion": notion,
        "ctx": builder.build(snap, now=NOW), "db": db, "vault": vault, "dir": vault_dir,
    })()
    store.close()


def executions(bot) -> list[dict]:
    con = sqlite3.connect(bot.db)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM executions ORDER BY id")]
    finally:
        con.close()


async def test_one_message_reaches_both_stores_and_one_undo_reverts_both(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("купить лампочки", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "купить лампочки",
                                         "heading": "дом", "tags": ["home"]}]}]

    reply = await bot.orch.handle_text(CHAT, USER, "купить лампочки")

    assert "Obsidian:" in reply.text  # the Notion answer, plus one line about the vault
    tasks = bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    assert "- [ ] купить лампочки #home" in tasks
    assert any(c[0] == "create_page" for c in bot.notion.calls)  # and Notion has its row

    [row] = executions(bot)
    undo = json.loads(row["undo"])
    assert undo["kind"] == "archive" and len(undo["vault"]) == 1

    undone = await bot.orch.handle_callback(CHAT, USER, f"u:{row['id']}")
    assert undone.text == texts.UNDONE
    assert "купить лампочки" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_the_vault_is_written_even_when_notion_asks_a_question(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                              fields={"t3.f1": val("зубы", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]}]

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert reply.buttons and "Obsidian:" in reply.text  # Notion is still asking
    assert "- [ ] зубы" in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")
    # Nothing was written to Notion, so the vault's undo gets a row of its own for /undo.
    [row] = executions(bot)
    undo = json.loads(row["undo"])
    assert undo["kind"] == "vault" and len(undo["vault"]) == 1

    undone = await bot.orch.undo(CHAT)
    assert undone.text == texts.UNDONE
    assert "зубы" not in bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md")


async def test_an_answer_to_a_question_does_not_reach_the_vault_again(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95,
                                              fields={"t3.f1": val("зубы", 1.0)})))
    bot.claude.answers = [{"actions": [{"action": "task", "text": "зубы", "heading": "дом"}]}]
    question = await bot.orch.handle_text(CHAT, USER, "зубы")
    assert question.buttons

    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    await bot.orch.handle_text(CHAT, USER, "приоритет A")

    assert len(bot.claude.seen) == 1  # the filer read the message, not the answer to Notion
    assert bot.index.read(f"{texts.VAULT_TASKS_NOTE}.md").count("- [ ] зубы") == 1


async def test_a_vault_failure_never_costs_the_notion_answer(bot):
    bot.llm.queue(make_interp("create", cand(bot.ctx, "t3", 0.95, fields={
        "t3.f1": val("зубы", 1.0), "t3.f2": val("t3.f2.o1", 1.0)})))
    bot.claude.answers = [RuntimeError("vault on fire")]

    reply = await bot.orch.handle_text(CHAT, USER, "зубы")

    assert "✅" in reply.text and reply.undo_id is not None  # Notion wrote and can be undone
    assert "Obsidian" not in reply.text  # nothing to report: the vault side never got that far
    assert json.loads(executions(bot)[0]["undo"])["vault"] == []
