"""app.main: build()'s wiring, startup_checks()'s fixed sequence and exit codes, and the
Sweeper's periodic-flush loop. All fakes, no network, no Telegram, no Ollama, no real Whisper, no
polling — build() takes fake provider/llm factories exactly so this suite never needs any of
those."""

from __future__ import annotations

import asyncio
import logging

import pytest

from app import main
from app.config import Settings
from app.notion.errors import NotionError
from tests.fakes import FakeLLM, FakeNotionProvider


def _page_provider(*, page_id: str = "pg-1", title: str = "Заметки") -> FakeNotionProvider:
    """A Notion workspace with exactly one page target — enough for Discovery to succeed without
    a single database/data-source in play."""
    provider = FakeNotionProvider()
    provider.search_results = [{
        "object": "page", "id": page_id, "url": f"https://notion.so/{page_id}",
        "last_edited_time": "2026-01-01T00:00:00.000Z",
        "parent": {"type": "workspace", "workspace": True},
        "properties": {"title": {"id": "title", "type": "title",
                                  "title": [{"plain_text": title}]}},
    }]
    return provider


def _build(env, *, provider=None, llm=None, admin_port: int = 0) -> main.App:
    env.setenv("ADMIN_UI_PORT", str(admin_port))
    settings = Settings()
    provider = provider if provider is not None else _page_provider()
    llm = llm if llm is not None else FakeLLM()
    return main.build(settings, provider_factory=lambda s: provider, llm_factory=lambda s: llm)


def _messages(caplog, logger_name: str = "app.main") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


# ---- startup_checks: exit codes -------------------------------------------------------------


async def test_exits_2_when_telegram_settings_missing(env):
    env.setenv("TELEGRAM_BOT_TOKEN", "")
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    app = _build(env)
    with pytest.raises(SystemExit) as exc:
        await main.startup_checks(app)
    assert exc.value.code == main.EXIT_CONFIG


async def test_exits_3_on_notion_401(env):
    provider = _page_provider()

    async def _unauthorized() -> dict:
        raise NotionError(401, "unauthorized", "API token is invalid.")

    provider.me = _unauthorized
    app = _build(env, provider=provider)
    app.store.migrate()
    with pytest.raises(SystemExit) as exc:
        await main.startup_checks(app)
    assert exc.value.code == main.EXIT_NOTION_AUTH


async def test_exits_4_when_migration_pending(env):
    app = _build(env)
    # Deliberately no app.store.migrate(): the fresh sqlite file starts with every migration
    # pending, exactly like a database nobody has run the updater against yet.
    with pytest.raises(SystemExit) as exc:
        await main.startup_checks(app)
    assert exc.value.code == main.EXIT_PENDING_MIGRATION


# ---- startup_checks: warnings, not exits ------------------------------------------------------


async def test_missing_ollama_model_warns_but_continues(env, caplog):
    env.setenv("LLM_MODEL", "definitely-not-pulled:7b")
    app = _build(env)
    app.store.migrate()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.startup_checks(app)  # must not raise
    assert any("definitely-not-pulled:7b" in m for m in _messages(caplog))


async def test_successful_run_logs_target_count_and_inbox_target(env, caplog):
    provider = _page_provider(page_id="pg-inbox", title="Входящие")
    env.setenv("INBOX_TARGET_ID", "pg-inbox")
    app = _build(env, provider=provider)
    app.store.migrate()
    with caplog.at_level(logging.INFO, logger="app.main"):
        await main.startup_checks(app)
    messages = _messages(caplog)
    assert any("1" in m and "target" in m for m in messages)
    assert any("Входящие" in m for m in messages)


async def test_no_inbox_flagged_warns_the_fallback_is_inert(env, caplog):
    app = _build(env)  # no INBOX_TARGET_ID, and nothing flagged in targets.yaml
    app.store.migrate()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.startup_checks(app)
    assert any("inbox" in m.lower() and "inert" in m.lower() for m in _messages(caplog))


# ---- admin server ------------------------------------------------------------------------------


async def test_admin_ui_port_zero_starts_no_server(env):
    app = _build(env, admin_port=0)
    app.store.migrate()
    await main.startup_checks(app)
    with pytest.raises(RuntimeError):
        _ = app.admin.port


async def test_admin_ui_port_positive_starts_a_server(env):
    app = _build(env, admin_port=18793)
    app.store.migrate()
    try:
        await main.startup_checks(app)
        assert app.admin.port == 18793
    finally:
        app.admin.stop()


# ---- sweeper -------------------------------------------------------------------------------


class _FlakyFlusher:
    """Raises on its first call, then succeeds — the sweeper must survive that and keep ticking,
    logging the failure rather than letting it kill the loop."""

    def __init__(self) -> None:
        self.calls = 0

    async def flush(self) -> int:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("boom")
        return self.calls


async def test_sweeper_ticks_repeatedly_and_survives_a_failed_run(caplog):
    flusher = _FlakyFlusher()
    sweeper = main.Sweeper(flusher.flush, interval_s=0.01)
    with caplog.at_level(logging.ERROR, logger="app.main"):
        sweeper.start()
        await asyncio.sleep(0.05)
        await sweeper.stop()
    assert flusher.calls >= 2
    assert any("sweep" in r.getMessage().lower() for r in caplog.records if r.name == "app.main")


async def test_sweeper_stop_is_idempotent_and_cancels_the_task():
    flusher = _FlakyFlusher()
    sweeper = main.Sweeper(flusher.flush, interval_s=0.01)
    sweeper.start()
    await asyncio.sleep(0.02)
    await sweeper.stop()
    calls_after_stop = flusher.calls
    await asyncio.sleep(0.05)
    await sweeper.stop()  # second stop: must not raise
    assert flusher.calls == calls_after_stop  # no further ticks once stopped
