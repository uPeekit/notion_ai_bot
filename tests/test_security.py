"""Security tests: what an adversarial LLM response, or an unauthorised Telegram user, can and
cannot do to this pipeline.

Every test wires the *real* collaborator under scrutiny (SemanticValidator, ContextBuilder,
Orchestrator, or the real `app.telegram.handlers`/`app.telegram.auth` gate) against fakes — never
a mock that only proves "was called". Assertions read what actually reached
`FakeNotionProvider.calls` (the payloads, not just the count), what `FakeLLM.seen` actually
recorded as the request context, what an actual `caplog` record actually says, or what rows an
actual temp-file `AuditStore` actually holds.

No test here touches the network, Telegram, Ollama, or loads a real Whisper model: Notion is
`FakeNotionProvider`/`FakeDiscovery`, the LLM is `FakeLLM`, speech is `FakeSpeech`
(`tests/test_handlers.py`), and dispatch walks the real registered handlers the way
`tests/test_handlers.py:dispatch` does — never `Application.process_update`, which would call
`Bot.get_me()`.

Two things the pipeline must never let an LLM response do, whatever it claims:
  * name a Notion id, a URL, or a field key the app itself did not mint as a context key, and have
    that reach `Executor`/`NotionProvider` (Section 1);
  * cause a Notion id, a URL, or either secret token to appear in what is sent back to the model
    itself, in the next request's context (Section 2).

And two things that must hold regardless of what the LLM ever sees:
  * neither the Notion token nor the Telegram bot token ever appears in a reply, an audit row, a
    log record at any level, or a provider call payload (Section 3);
  * a user outside `TELEGRAM_ALLOWED_USER_IDS` gets no reply, earns no audit row, and triggers no
    provider call at all (Section 3).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app import logging_setup, main, texts
from app.config import Settings
from app.llm.context import ContextBuilder
from tests.fakes import FakeLLM, FakeNotionProvider
from tests.helpers import cand, make_interp, val
from tests.test_handlers import (
    ALLOWED_USER,
    DENIED_USER,
    FakeBot,
    FakeSpeech,
    command_update,
    dispatch,
    text_update,
)
from tests.test_logging import _reset_root_and_third_party
from tests.test_orchestrator import CHAT, USER, Bot, make_bot, notion_calls

# Two distinct, obviously-fake secrets, chosen so a substring match can never be an accident.
NOTION_SECRET = "ntn-SECRET-b7f0c2e4a19d"
TELEGRAM_SECRET = "918273645:AA-TELEGRAM-SECRET-f00dcafe1234567890ab"


# ==== Section 1: a crafted Interpretation never reaches the provider ============================
#
# `Candidate.target`/`.item`/`.fields` keys (app/interpretation/models.py) are plain `str` — the
# *grammar* Ollama is given constrains them to context keys, but nothing stops a model (or a
# hand-rolled `format=json` request bypassing the grammar) from returning a real Notion id, a
# Notion URL, or a field key the app never advertised. `SemanticValidator` is the second, always-
# enforced line: every one of these must be caught there, before `build_command`/`Executor` ever
# sees the candidate. `inbox_mode="off"` keeps the assertion crisp: zero provider calls, not "only
# the legitimate ones".


@pytest.fixture
def clean(tmp_path) -> Bot:
    bot = make_bot(tmp_path, inbox_mode="off")
    yield bot
    bot.store.close()


async def test_foreign_notion_id_as_target_is_rejected_before_reaching_notion(clean):
    """`cand.target` is a real Notion data-source id ("ds-buy") instead of a context key ("t2") —
    exactly what a compromised/careless model could return. `ctx.ref("ds-buy")` finds nothing, so
    `SemanticValidator` issues `SEM_UNKNOWN_KEY` and drops the candidate; the only candidate here,
    so the turn REJECTs with none. No `create_page`/`update_page`/`append_blocks` call is made."""
    clean.llm.queue(make_interp("create", cand(clean.ctx, "ds-buy", 0.95)))

    reply = await clean.orch.handle_text(CHAT, USER, "любой текст")

    assert reply.text == texts.ERRORS["SEM_UNKNOWN_KEY"]
    assert clean.notion.calls == []


async def test_url_as_target_is_rejected_before_reaching_notion(clean):
    """The same attack, shaped as a Notion URL rather than a raw id — still not a context key."""
    clean.llm.queue(make_interp("create", cand(clean.ctx, "https://notion.so/ds-buy", 0.95)))

    reply = await clean.orch.handle_text(CHAT, USER, "любой текст")

    assert reply.text == texts.ERRORS["SEM_UNKNOWN_KEY"]
    assert clean.notion.calls == []


async def test_foreign_item_id_is_rejected_before_reaching_notion(clean):
    """`cand.item` names a real Notion page id ("b-milk") instead of an item key ("t2.i2"). The
    target itself is valid; only the item reference is forged. `SemanticValidator._item` looks it
    up via `ctx.item_keys`, finds nothing, and the whole candidate is dropped the same way."""
    clean.llm.queue(make_interp(
        "update", cand(clean.ctx, "t2", 0.95, item="b-milk",
                      fields={"t2.f5": val(True, 1.0)}),
    ))

    reply = await clean.orch.handle_text(CHAT, USER, "отметь молоко купленным")

    assert reply.text == texts.ERRORS["SEM_UNKNOWN_KEY"]
    assert clean.notion.calls == []


async def test_extra_field_key_is_rejected_before_reaching_notion(clean):
    """A field key the app never put in the context (`t2.f99`, or an attacker-shaped id-looking
    string) sitting alongside otherwise-valid fields. `SemanticValidator` walks `cand.fields` and
    rejects the *whole* candidate the moment one key is not in `ctx.field_keys(target)` — it does
    not just drop the unknown key and keep the rest, so a smuggled extra key cannot ride along
    with a legitimate write."""
    c = cand(clean.ctx, "t2", 0.95, fields={"t2.f1": val("Молоко", 1.0)})
    c["fields"]["t2.f99"] = val("не должно попасть в Notion", 1.0)
    clean.llm.queue(make_interp("create", c))

    reply = await clean.orch.handle_text(CHAT, USER, "купи молоко")

    assert reply.text == texts.ERRORS["SEM_UNKNOWN_KEY"]
    assert clean.notion.calls == []


async def test_valid_candidate_control_still_executes(clean):
    """Positive control for the four tests above: with only the forged key/id/url removed, the
    identical pipeline executes normally. Without this, a bug that made the validator reject
    *everything* would pass every test above for the wrong reason."""
    clean.llm.queue(make_interp("create", cand(
        clean.ctx, "t2", 0.95, fields={"t2.f1": val("Молоко", 1.0)},
    )))

    reply = await clean.orch.handle_text(CHAT, USER, "купи молоко")

    assert "Молоко" in reply.text
    assert notion_calls(clean, "create_page") != []


# ==== Section 2: the LLM context never carries a Notion id, a URL, or a token ====================
#
# documentation/ARCHITECTURE.md §14: "LLM receives: context JSON, user text, time. Never tokens,
# ids (only keys), URLs, tool lists." `FakeLLM.seen` records a deep copy of exactly the payload
# `ContextBuilder.build()` produced (see `Context.json()` / `Orchestrator._text`), so this is not
# an assumption about the code — it is a scan of what a request would actually have sent to
# Ollama, across a full clarify round-trip (so the `pending` block introduced in F4/F5 is covered
# too, not just the first turn).


def _real_ids_and_urls(snapshot) -> list[str]:
    """Every Notion id and URL that exists in the fixture workspace — the exact values the context
    payload must never contain, however they got embedded (target id, item id, page url)."""
    values: list[str] = []
    for t in snapshot.targets:
        values.append(t.id)
        values.append(t.url)
        if t.database_id:
            values.append(t.database_id)
        for it in t.items:
            values.append(it.id)
            values.append(it.url)
    return values


async def test_llm_context_never_contains_a_notion_id_or_url(clean):
    clean.llm.queue(make_interp(
        "create",
        cand(clean.ctx, "t2", 0.88, fields={"t2.f1": val("Хлеб", 1.0)}),
        cand(clean.ctx, "t3", 0.82, fields={"t3.f1": val("Хлеб", 1.0)}),
    ))
    await clean.orch.handle_text(CHAT, USER, "добавь хлеб")

    assert clean.llm.calls == 1
    _, context_payload, _ = clean.llm.seen[0]
    payload_json = json.dumps(context_payload, ensure_ascii=False)
    for value in _real_ids_and_urls(clean.snapshot):
        assert value not in payload_json, f"leaked {value!r} into the LLM context"
    assert "http://" not in payload_json and "https://" not in payload_json
    assert NOTION_SECRET not in payload_json


async def test_pending_block_context_never_contains_a_notion_id_or_context_key(clean):
    """F4/F5: a live session adds a `pending` block (question text, target name, original text —
    app/conversation/orchestrator.py:_pending_block) to the *second* LLM call. That block is built
    from names only; this proves the second request's full context — pending block included —
    still carries no id, url, or raw context key that would let the model learn something about
    the app's internal addressing rather than the workspace as presented."""
    clean.llm.queue(make_interp(
        "create", cand(clean.ctx, "t3", 0.95, fields={"t3.f1": val("Подготовить документы", 1.0)}),
    ))
    await clean.orch.handle_text(CHAT, USER, "добавь задачу подготовить документы")
    assert clean.llm.calls == 1  # the required-field question is now pending

    clean.llm.queue(make_interp(
        "create", cand(clean.ctx, "t3", 0.95, fields={
            "t3.f1": val("Подготовить документы", 1.0), "t3.f2": val("t3.f2.o1", 1.0),
        }),
    ))
    await clean.orch.handle_text(CHAT, USER, "высокий")

    assert clean.llm.calls == 2
    _, context_payload, _ = clean.llm.seen[1]
    assert "pending" in context_payload
    payload_json = json.dumps(context_payload, ensure_ascii=False)
    for value in _real_ids_and_urls(clean.snapshot):
        assert value not in payload_json, f"leaked {value!r} into the pending-turn LLM context"
    assert "http://" not in payload_json and "https://" not in payload_json


# ==== Section 3: full-stack — real Settings/App, real Telegram auth gate, real audit store =======
#
# `app.main.build()` is exercised (not just the Orchestrator) so `app.settings` genuinely holds
# both secrets as `SecretStr`s the whole process lives with, `app.telegram_app` carries the real
# `allowed_filter` gate (app/telegram/auth.py), and `app.store` is a real on-disk `AuditStore` —
# the same three components a live deployment actually runs. Only the Notion provider, the LLM,
# and speech are fakes; nothing here touches the network or loads a real Whisper model.


def _rich(text: str) -> list[dict]:
    return [{"plain_text": text}]


def _page(id_: str, title: str, parent: dict) -> dict:
    return {"object": "page", "id": id_, "url": f"https://notion.so/{id_}",
            "last_edited_time": "2026-09-01T00:00:00.000Z", "parent": parent,
            "properties": {"title": {"id": "title", "type": "title", "title": _rich(title)}}}


def _shopping_list_provider() -> FakeNotionProvider:
    """One writable database, "Покупки", with a title property only — just enough for a create
    flow to reach `Executor.run` and come back with an undoable page."""
    f = FakeNotionProvider()
    f.search_results = [
        {"object": "data_source", "id": "ds-shop",
         "parent": {"type": "database_id", "database_id": "db-shop"}},
    ]
    f.databases = {"db-shop": {"id": "db-shop", "parent": {"type": "workspace", "workspace": True}}}
    f.data_sources = {
        "ds-shop": {
            "id": "ds-shop", "title": _rich("Покупки"), "description": [],
            "url": "https://notion.so/ds-shop", "parent": {"database_id": "db-shop"},
            "properties": {"Название": {"id": "title", "type": "title"}},
        },
    }
    f.items = {"ds-shop": []}
    return f


def _build_full_app(env, *, provider: FakeNotionProvider, llm: FakeLLM) -> main.App:
    env.setenv("NOTION_TOKEN", NOTION_SECRET)
    env.setenv("TELEGRAM_BOT_TOKEN", TELEGRAM_SECRET)
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", f"{ALLOWED_USER},2")
    env.setenv("ADMIN_UI_PORT", "0")
    settings = Settings()
    app = main.build(
        settings, provider_factory=lambda s: provider, llm_factory=lambda s: llm,
        speech_factory=lambda s: FakeSpeech(),
    )
    app.store.migrate()
    return app


def _create_command_interp(app: main.App, snapshot) -> object:
    ctx = ContextBuilder(app.settings.timezone, app.settings.items_per_target).build(
        snapshot, now=datetime.now(UTC)
    )
    return make_interp("create", cand(ctx, "t1", 0.95, fields={"t1.f1": val("Молоко", 1.0)}))


def _events(app: main.App) -> list[dict]:
    con = sqlite3.connect(app.settings.db_path)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("SELECT * FROM events ORDER BY rowid")]
    finally:
        con.close()


async def test_unauthorized_user_produces_no_audit_row_and_no_provider_call(env):
    app = _build_full_app(env, provider=_shopping_list_provider(), llm=FakeLLM())
    fbot = FakeBot()
    context = SimpleNamespace(bot_data=app.telegram_app.bot_data, bot=fbot, error=None)
    update = text_update(DENIED_USER, "купи молоко", fbot)

    dispatched = await dispatch(app.telegram_app, update, context)

    assert dispatched is False
    assert fbot.sent == []
    assert _events(app) == []
    assert app.provider.calls == []


async def test_authorized_user_control_produces_a_row_and_a_provider_call(env):
    """Positive control for the test above: without it, a bug that gated *every* user (not just
    unauthorised ones) would pass the unauthorised-user test for the wrong reason."""
    provider = _shopping_list_provider()
    llm = FakeLLM()
    app = _build_full_app(env, provider=provider, llm=llm)
    snapshot = await app.discovery.get()
    llm.queue(_create_command_interp(app, snapshot))
    fbot = FakeBot()
    context = SimpleNamespace(bot_data=app.telegram_app.bot_data, bot=fbot, error=None)
    update = text_update(ALLOWED_USER, "купи молоко", fbot)

    dispatched = await dispatch(app.telegram_app, update, context)

    assert dispatched is True
    assert len(fbot.sent) == 1
    assert len(_events(app)) == 1
    assert [c for c in provider.calls if c[0] == "create_page"] != []


async def test_neither_token_leaks_into_reply_audit_row_or_provider_payload(env):
    provider = _shopping_list_provider()
    llm = FakeLLM()
    app = _build_full_app(env, provider=provider, llm=llm)
    snapshot = await app.discovery.get()
    llm.queue(_create_command_interp(app, snapshot))
    fbot = FakeBot()
    context = SimpleNamespace(bot_data=app.telegram_app.bot_data, bot=fbot, error=None)

    await dispatch(app.telegram_app, text_update(ALLOWED_USER, "купи молоко", fbot), context)
    await dispatch(app.telegram_app, command_update(ALLOWED_USER, "undo", fbot), context)

    assert len(fbot.sent) == 2
    for sent in fbot.sent:
        assert NOTION_SECRET not in sent["text"]
        assert TELEGRAM_SECRET not in sent["text"]
    for event in _events(app):
        blob = json.dumps(event, ensure_ascii=False, default=str)
        assert NOTION_SECRET not in blob
        assert TELEGRAM_SECRET not in blob
    for call in provider.calls:
        blob = json.dumps(call, ensure_ascii=False, default=str)
        assert NOTION_SECRET not in blob
        assert TELEGRAM_SECRET not in blob


async def test_log_capture_over_text_execute_undo_has_no_token_and_no_message_above_debug(
    env, capsys
):
    """One full flow under the real `app.logging_setup.configure("DEBUG")` — an operator's
    `LOG_LEVEL=DEBUG` in `.env` — which is what makes this test worth having: at DEBUG,
    `Application.builder().token(...).build()` (constructing PTB's own `ExtBot`) logs
    `"Set Bot API URL: https://api.telegram.org/bot<TOKEN>"` straight to stderr from its own
    constructor, a leak `httpx`/`httpcore` silencing alone does not cover (`ExtBot` is not an
    httpx request) — see `app/logging_setup.py`'s module docstring for the full story. Then: an
    unauthorised attempt, a text create, and `/undo`.

    `capsys` (real stderr), not `caplog`, is deliberate: `configure()` installs its own
    `StreamHandler` on the root logger, replacing whatever was there — calling it while relying on
    `caplog`'s handler would tear that handler out from under the fixture. `tests/test_logging.py`
    already established this exact pattern (`test_configure_silences_httpx_and_httpcore_info_
    logging`); `_reset_root_and_third_party` (same module) restores global logging state in a
    `finally`, so this test's `configure()` call cannot bleed into any test that runs after it.

    A clean EXECUTE + undo flow logs nothing on its own (no news is the security property here),
    so the denied-user attempt is folded in to give the capture at least one real record — the
    `AUTH_DENIED` warning — to scan; without it this test would vacuously pass over empty output."""
    message_text = "купи молоко"
    logging_setup.configure("DEBUG")
    try:
        provider = _shopping_list_provider()
        llm = FakeLLM()
        app = _build_full_app(env, provider=provider, llm=llm)  # constructs ExtBot under DEBUG
        snapshot = await app.discovery.get()
        llm.queue(_create_command_interp(app, snapshot))
        fbot = FakeBot()
        context = SimpleNamespace(bot_data=app.telegram_app.bot_data, bot=fbot, error=None)

        await dispatch(app.telegram_app, text_update(DENIED_USER, message_text, fbot), context)
        await dispatch(app.telegram_app, text_update(ALLOWED_USER, message_text, fbot), context)
        await dispatch(app.telegram_app, command_update(ALLOWED_USER, "undo", fbot), context)
        err = capsys.readouterr().err
    finally:
        _reset_root_and_third_party()

    assert err  # the test would prove nothing over empty output
    assert "AUTH_DENIED" in err
    assert NOTION_SECRET not in err
    assert TELEGRAM_SECRET not in err
    for line in err.splitlines():
        if not line.strip():
            continue
        level_name = line.split()[2]  # FORMAT: "<date> <time> <LEVEL> <name> [event=..] <msg>"
        if level_name != "DEBUG":
            assert message_text not in line
