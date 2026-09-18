"""app.main: build()'s wiring, the two-phase startup check split, and the Sweeper's periodic-
flush loop. All fakes, no network, no Telegram, no Ollama, no real Whisper, no polling —
build() takes fake provider/llm factories exactly so this suite never needs any of those.

`startup_checks` (settings + schema, exit 2/4) is synchronous and tested directly.
`post_init_checks` (Ollama, Notion, discovery, admin — everything that needs a running event
loop) is what `Application.post_init` calls; it is tested by awaiting it directly, exactly as
`build()` wires it, never through `run_polling()`."""

from __future__ import annotations

import asyncio
import logging
import sqlite3

import pytest
from pydantic import ValidationError
from telegram.error import InvalidToken

from app import main, texts
from app.config import Settings
from app.notion.errors import NotionError
from tests.fakes import FakeLLM, FakeNotionProvider

# Captured at import, before stub_command_menu monkeypatches the name: the two tests that
# exercise the real command registration call this instead of the stubbed attribute.
_REAL_REGISTER_COMMANDS = main._register_commands

# The two secret values tests.conftest's `env` fixture puts in the environment.
TELEGRAM_TOKEN = "tg-test-token"
NOTION_TOKEN = "ntn-test-token"


class FakeTelegramBot:
    """Stands in for `Application.bot` in post_init_checks: the real ExtBot would put
    set_my_commands on the wire, and no test here goes near the network."""

    def __init__(self) -> None:
        self.commands = None

    async def set_my_commands(self, commands) -> None:
        self.commands = commands


class FakeApplication:
    """The two members post_init_checks touches on the PTB Application it is handed."""

    def __init__(self) -> None:
        self.bot = FakeTelegramBot()
        self.stopped = False

    def stop_running(self) -> None:
        self.stopped = True


@pytest.fixture(autouse=True)
def stub_command_menu(monkeypatch):
    """post_init_checks publishes the Telegram command menu, which is a real API call. Every test
    here stubs it out and records the calls; `_register_commands` itself is exercised directly by
    test_register_commands_publishes_every_command below."""
    calls = []

    async def _stub(bot) -> None:
        calls.append(bot)

    monkeypatch.setattr(main, "_register_commands", _stub)
    return calls


@pytest.fixture
def clean_logging():
    """main() reconfigures the root logger; put it back so the rest of the suite is unaffected."""
    yield
    for h in list(logging.getLogger().handlers):
        logging.getLogger().removeHandler(h)


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


# ---- startup_checks (sync): exit codes 2 and 4 -------------------------------------------------


def test_exits_2_when_telegram_settings_missing(env):
    env.setenv("TELEGRAM_BOT_TOKEN", "")
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", "")
    app = _build(env)
    with pytest.raises(SystemExit) as exc:
        main.startup_checks(app)
    assert exc.value.code == main.EXIT_CONFIG


def test_exits_4_when_migration_pending(env):
    app = _build(env)
    # Deliberately no app.store.migrate(): the fresh sqlite file starts with every migration
    # pending, exactly like a database nobody has run the updater against yet.
    with pytest.raises(SystemExit) as exc:
        main.startup_checks(app)
    assert exc.value.code == main.EXIT_PENDING_MIGRATION


def test_startup_checks_passes_through_when_settings_and_schema_are_fine(env):
    app = _build(env)
    app.store.migrate()
    main.startup_checks(app)  # must not raise


# ---- Important 1: the exit-2 message never carries raw ValidationError text --------------------


def test_config_error_message_never_leaks_validation_error_text(env):
    env.delenv("NOTION_TOKEN", raising=False)  # required, no default -> Settings() fails to build
    with pytest.raises(ValidationError) as exc:
        Settings()
    message = main._config_error_message(exc.value)
    assert "input_value" not in message
    assert "notion_token" in message.lower()


# ---- post_init_checks: exit-3-equivalent (records app.fatal, does not sys.exit) -----------------


async def test_notion_401_records_fatal_and_stops_before_discovery(env):
    provider = _page_provider()

    async def _unauthorized() -> dict:
        raise NotionError(401, "unauthorized", "API token is invalid.")

    provider.me = _unauthorized
    app = _build(env, provider=provider, admin_port=18794)
    app.store.migrate()

    await main.post_init_checks(app, app.telegram_app)  # must not raise

    assert app.fatal == (
        main.EXIT_NOTION_AUTH,
        "Notion authentication failed (401 unauthorized: API token is invalid.). "
        "Check NOTION_TOKEN in .env.",
    )
    assert app.discovery.last is None  # discovery never ran: the check returned early
    with pytest.raises(RuntimeError):
        _ = app.admin.port  # admin never started either


async def test_notion_5xx_only_warns_and_continues(env, caplog):
    provider = _page_provider()

    async def _server_error() -> dict:
        raise NotionError(500, "internal_server_error", "boom")

    provider.me = _server_error
    app = _build(env, provider=provider)
    app.store.migrate()

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.post_init_checks(app, app.telegram_app)  # must not raise, must not set fatal

    assert app.fatal is None
    assert any("notion check failed" in m for m in _messages(caplog))
    assert app.discovery.last is not None  # startup continued past the warning into discovery


# ---- post_init_checks: warnings, not fatal ------------------------------------------------------


async def test_missing_ollama_model_warns_but_continues(env, caplog):
    env.setenv("LLM_MODEL", "definitely-not-pulled:7b")
    app = _build(env)
    app.store.migrate()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.post_init_checks(app, app.telegram_app)  # must not raise
    assert any("definitely-not-pulled:7b" in m for m in _messages(caplog))


async def test_successful_run_logs_target_count_and_inbox_target(env, caplog):
    provider = _page_provider(page_id="pg-inbox", title="Входящие")
    env.setenv("INBOX_TARGET_ID", "pg-inbox")
    app = _build(env, provider=provider)
    app.store.migrate()
    with caplog.at_level(logging.INFO, logger="app.main"):
        await main.post_init_checks(app, app.telegram_app)
    messages = _messages(caplog)
    assert any("1" in m and "target" in m for m in messages)
    assert any("Входящие" in m for m in messages)


async def test_no_inbox_flagged_warns_the_fallback_is_inert(env, caplog):
    app = _build(env)  # no INBOX_TARGET_ID, and nothing flagged in targets.yaml
    app.store.migrate()
    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.post_init_checks(app, app.telegram_app)
    assert any("inbox" in m.lower() and "inert" in m.lower() for m in _messages(caplog))


# ---- admin server ------------------------------------------------------------------------------


async def test_admin_ui_port_zero_starts_no_server(env):
    app = _build(env, admin_port=0)
    app.store.migrate()
    await main.post_init_checks(app, app.telegram_app)
    with pytest.raises(RuntimeError):
        _ = app.admin.port


async def test_admin_ui_port_positive_starts_a_server(env):
    app = _build(env, admin_port=18793)
    app.store.migrate()
    try:
        await main.post_init_checks(app, app.telegram_app)
        assert app.admin.port == 18793
    finally:
        app.admin.stop()


# ---- Important 3: the post_init/post_shutdown contract, with no polling needed to test it ------


def test_build_attaches_post_init_and_post_shutdown_hooks(env):
    """Guards against a typo (e.g. wiring `post_stop` instead of `post_shutdown`) passing
    silently: build() must actually attach both, not just intend to."""
    app = _build(env)
    assert app.telegram_app.post_init is not None
    assert app.telegram_app.post_shutdown is not None


async def test_post_init_starts_the_sweeper(env):
    app = _build(env, admin_port=0)
    app.store.migrate()
    assert not app.sweeper.running
    await app.telegram_app.post_init(app.telegram_app)
    try:
        assert app.sweeper.running
    finally:
        await app.sweeper.stop()


async def test_post_init_does_not_start_the_sweeper_on_a_fatal_notion_failure(env):
    provider = _page_provider()

    async def _unauthorized() -> dict:
        raise NotionError(401, "unauthorized", "nope")

    provider.me = _unauthorized
    app = _build(env, provider=provider)
    app.store.migrate()
    await app.telegram_app.post_init(app.telegram_app)
    assert not app.sweeper.running


async def test_post_shutdown_stops_sweeper_stops_admin_and_closes_store(env):
    app = _build(env, admin_port=18795)
    app.store.migrate()
    await app.telegram_app.post_init(app.telegram_app)
    assert app.sweeper.running
    assert app.admin.port == 18795

    await app.telegram_app.post_shutdown(app.telegram_app)

    assert not app.sweeper.running
    with pytest.raises(RuntimeError):
        _ = app.admin.port
    with pytest.raises(sqlite3.ProgrammingError):
        app.store.get_event(1)  # any call through the closed connection raises


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


# ---- Critical: a token Telegram rejects must not reach stderr ----------------------------------


def test_invalid_telegram_token_exits_2_instead_of_printing_a_traceback(
    env, monkeypatch, capsys, clean_logging
):
    """PTB raises `InvalidToken` out of run_polling() when Telegram refuses the token, and builds
    that exception's message by interpolating the token into it ("The token `<token>` was rejected
    by the server."). Uncaught, Python prints the whole traceback — and README tells the user to
    redirect stderr into logs\bot.log, so it lands on disk. main() must turn it into a config
    exit whose message names the key and never the value."""
    app = _build(env)
    app.store.migrate()

    def _rejected(*_args, **_kwargs):
        raise InvalidToken(f"The token `{TELEGRAM_TOKEN}` was rejected by the server.")

    monkeypatch.setattr(app.telegram_app, "run_polling", _rejected)
    monkeypatch.setattr(main, "build", lambda settings, **_kw: app)

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == main.EXIT_CONFIG
    err = capsys.readouterr().err
    assert TELEGRAM_TOKEN not in err
    assert "Traceback" not in err
    assert "TELEGRAM_BOT_TOKEN" in err


def test_main_passes_both_token_values_to_the_log_redactor(env, monkeypatch, clean_logging):
    """The redaction only works if main() actually hands `configure` the two secret values — and
    nothing else, no Settings object."""
    app = _build(env)
    app.store.migrate()
    recorded = {}

    def _configure(level, *, redact=(), log_file=None):
        recorded["level"] = level
        recorded["redact"] = tuple(redact)
        recorded["log_file"] = log_file

    monkeypatch.setattr(main, "configure", _configure)
    monkeypatch.setattr(app.telegram_app, "run_polling", lambda *a, **k: None)
    monkeypatch.setattr(main, "build", lambda settings, **_kw: app)

    main.main()

    assert recorded["redact"] == (TELEGRAM_TOKEN, NOTION_TOKEN)
    assert recorded["log_file"] == "logs/bot.log"  # the default reaches the file handler


# ---- one bot per install ----------------------------------------------------------------------


def test_second_instance_exits_5_without_building(env, monkeypatch, capsys, clean_logging):
    """Double-clicking start.cmd twice is the easy mistake: two pollers on one token get
    Telegram 409 Conflicts and race each other's sessions in one database."""
    from app.config import Settings
    from app.instance_lock import InstanceLock

    held = InstanceLock(Settings().db_path.parent / "bot.pid")
    held.acquire()
    monkeypatch.setattr(main, "build", lambda settings, **_kw: pytest.fail("never reached"))
    try:
        with pytest.raises(SystemExit) as exc:
            main.main()
    finally:
        held.release()

    assert exc.value.code == main.EXIT_ALREADY_RUNNING
    assert "already running" in capsys.readouterr().err


def test_lock_is_released_when_main_exits(env, monkeypatch, clean_logging):
    from app.config import Settings
    from app.instance_lock import is_locked

    app = _build(env)
    app.store.migrate()
    monkeypatch.setattr(app.telegram_app, "run_polling", lambda *a, **k: None)
    monkeypatch.setattr(main, "build", lambda settings, **_kw: app)

    main.main()

    assert not is_locked(Settings().db_path.parent / "bot.pid")


def test_lock_is_released_on_a_fatal_exit_too(env, monkeypatch, clean_logging):
    from app.config import Settings
    from app.instance_lock import is_locked

    app = _build(env)
    app.store.migrate()

    def _rejected(*_args, **_kwargs):
        raise InvalidToken("rejected")

    monkeypatch.setattr(app.telegram_app, "run_polling", _rejected)
    monkeypatch.setattr(main, "build", lambda settings, **_kw: app)

    with pytest.raises(SystemExit):
        main.main()

    assert not is_locked(Settings().db_path.parent / "bot.pid")


# ---- Important 1: an unusable LOG_LEVEL is a config error, not a crash -------------------------


def test_invalid_log_level_exits_2_naming_the_valid_levels(env, monkeypatch, capsys):
    """`LOG_LEVEL` is the one key the README invites the user to edit while debugging, and
    `Logger.setLevel("info")` raises a bare ValueError from the first line of main()."""
    env.setenv("LOG_LEVEL", "verbose")
    monkeypatch.setattr(main, "build", lambda settings, **_kw: pytest.fail("never reached"))

    with pytest.raises(SystemExit) as exc:
        main.main()

    assert exc.value.code == main.EXIT_CONFIG
    err = capsys.readouterr().err
    assert "verbose" in err
    assert "DEBUG" in err and "INFO" in err


def test_lowercase_log_level_is_accepted(env, monkeypatch, clean_logging):
    env.setenv("LOG_LEVEL", "debug")
    app = _build(env)
    app.store.migrate()
    monkeypatch.setattr(app.telegram_app, "run_polling", lambda *a, **k: None)
    monkeypatch.setattr(main, "build", lambda settings, **_kw: app)

    main.main()

    assert logging.getLogger().level == logging.DEBUG


# ---- Important 2: a failed admin bind must not take the bot down ------------------------------


async def test_admin_bind_failure_only_warns_and_startup_continues(env, caplog):
    """`WinError 10013` on 8787 (an excluded port range, or something already listening) is a
    routine Windows failure. Unguarded it escapes post_init, past Application.__run's
    `except (KeyboardInterrupt, SystemExit)`, and out of run_polling() — losing the whole bot for
    the sake of a page that ADMIN_UI_PORT=0 would have turned off anyway."""
    app = _build(env, admin_port=18796)
    app.store.migrate()

    def _bind_fails() -> None:
        raise OSError(10013, "An attempt was made to access a socket in a forbidden way")

    app.admin.start = _bind_fails

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main.post_init_checks(app, FakeApplication())  # must not raise

    assert app.fatal is None
    assert any("admin" in m.lower() and "18796" in m for m in _messages(caplog))


# ---- the Telegram command menu ----------------------------------------------------------------


async def test_post_init_publishes_the_command_menu(env, stub_command_menu):
    app = _build(env)
    app.store.migrate()
    application = FakeApplication()

    await main.post_init_checks(app, application)

    assert stub_command_menu == [application.bot]


async def test_register_commands_publishes_every_command():
    """Without set_my_commands, Telegram shows no "/" menu at all and the user has to remember
    every command unaided."""
    bot = FakeTelegramBot()

    await _REAL_REGISTER_COMMANDS(bot)

    assert [c.command for c in bot.commands] == list(texts.COMMANDS)
    assert [c.description for c in bot.commands] == list(texts.COMMANDS.values())


async def test_register_commands_survives_an_unreachable_telegram(caplog):
    """Not fatal, and the log line carries the exception class only — an InvalidToken raised here
    would carry the bot token in its message."""

    class _Failing:
        async def set_my_commands(self, commands):
            raise InvalidToken("The token `123:SECRET` was rejected by the server.")

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await _REAL_REGISTER_COMMANDS(_Failing())  # must not raise

    messages = _messages(caplog)
    assert any("command menu" in m for m in messages)
    assert not any("SECRET" in m for m in messages)
