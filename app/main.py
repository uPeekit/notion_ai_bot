"""Application wiring, startup checks, and the process entry point.

Everything else in this codebase is a class waiting to be constructed; this module is where that
finally happens, once, for the single long-running process a user starts on their own machine
(`deploy/run.ps1`: `python -m app.main`). `build()` constructs every collaborator and returns them
on one `App`. Startup checks come in two parts, run from two different places, and that split is
load-bearing rather than cosmetic:

* `startup_checks(app)` — settings (exit 2) and schema (exit 4) — is synchronous and runs in
  `main()` before any event loop exists.
* `post_init_checks(app, application)` — Ollama, Notion, initial discovery, the Telegram command
  menu, the admin server — is async and runs from `Application.post_init`, i.e. inside
  python-telegram-bot's own event loop, the same one that later does the real polling.

The split exists because of a real, reproduced failure mode: running the async checks in a
throwaway `asyncio.run(...)` before calling `Application.run_polling()` leaves the very same
httpx-backed clients (`DirectNotionProvider`, `OllamaClient`) holding idle keep-alive connections
bound to that now-closed loop; the next real call on them — the first Notion write or the first
LLM call of the run — then raises `RuntimeError: Event loop is closed`, self-heals on retry, and
reads like a random flake instead of the systematic bug it is. Running those checks inside
`post_init` keeps every use of those clients on the one loop `run_polling()` owns for the whole
process lifetime, so this never happens. `documentation/ERRORS.md`'s "settings -> schema ->
Ollama -> Notion -> discovery -> admin -> polling" ordering is preserved either way: only *when*
each step runs relative to the event loop's existence changed, not the sequence itself.

Startup/exit messages on stderr are host-side operator text, not the Russian user-facing UI in
`app/texts.py`, and stay in English on purpose — this module must contain no Cyrillic literal.

Migrations are never applied here (documentation/ARCHITECTURE.md section 15):
`AuditStore.assert_schema_current()` refuses to run against a pending migration, and this
module's only reaction to that is to print the hint it already carries and exit 4. Nothing here
ever calls `AuditStore.migrate()` — that is the installer/updater's job (or `tools/migrate.py`
run by hand), never something the bot does to itself on the way up.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from typing import NoReturn

from telegram import Bot, BotCommand
from telegram.error import InvalidToken
from telegram.ext import Application

from app import texts
from app.admin.server import AdminServer
from app.audit.migrate import MigrationError
from app.audit.store import AuditStore
from app.commands.executor import Executor
from app.config import Settings, load_settings
from app.conversation.orchestrator import Orchestrator
from app.conversation.session import SessionStore
from app.instance_lock import AlreadyRunning, InstanceLock
from app.llm.base import LLMClient, LLMError
from app.llm.claude import ClaudeClient
from app.llm.context import ContextBuilder
from app.llm.fallback import FallbackLLM
from app.llm.ollama import OllamaClient
from app.llm.research import WebResearcher
from app.logging_setup import configure
from app.notion.descriptions import Descriptions, WorkspaceNote
from app.notion.direct import DirectNotionProvider
from app.notion.discovery import Discovery
from app.notion.errors import NotionError
from app.notion.images import ImageHost
from app.notion.provider import NotionProvider
from app.speech.base import SpeechToText
from app.speech.whisper_local import WhisperLocal
from app.telegram.handlers import register
from app.validation.policy import Policy, Thresholds
from app.validation.semantic import SemanticValidator

log = logging.getLogger(__name__)

EXIT_CONFIG = 2
EXIT_NOTION_AUTH = 3
EXIT_PENDING_MIGRATION = 4
EXIT_ALREADY_RUNNING = 5

_ALREADY_RUNNING = (
    "the bot is already running for this install (look for its other window). "
    "Close that one first - two copies would fight over Telegram and the database."
)

# Only an actual auth failure is fatal (documentation/ERRORS.md: "exit 3 on 401"; 403 reads the
# same way — a token that is simply forbidden rather than merely absent). Anything else from
# provider.me() (a 5xx, a timeout, NotionUnavailable's status=0) is a transient-looking failure,
# not proof the token is wrong, and is treated like the Ollama check right above it: a WARNING,
# not a reason to kill a process the user may otherwise be able to use just fine once Notion (or
# the network) recovers.
_NOTION_AUTH_STATUSES = frozenset({401, 403})

# Non-empty so python-telegram-bot's Bot() constructor (which only ever checks truthiness, never
# format) accepts it when the real token is blank. Real validity is settings.require_telegram()'s
# job — the very first startup check, which exits the process long before polling would ever put
# this placeholder in front of Telegram.
_PLACEHOLDER_TOKEN = "0:MISSING-TELEGRAM-BOT-TOKEN"

# What main() prints when Telegram itself rejects the token. Fixed text, never `str(e)` and never
# `exc_info`: python-telegram-bot builds `InvalidToken`'s message by interpolating the token into
# it (`telegram/_bot.py`: "The token `<token>` was rejected by the server."), so printing or
# logging that exception is exactly the leak this message exists to avoid.
_TELEGRAM_TOKEN_REJECTED = (
    "configuration error: Telegram rejected the bot token. Check TELEGRAM_BOT_TOKEN in .env "
    "(re-issue it with @BotFather if it was revoked) and restart."
)

ProviderFactory = Callable[[Settings], NotionProvider]
LLMFactory = Callable[[Settings], LLMClient]
SpeechFactory = Callable[[Settings], SpeechToText]


def _default_provider(settings: Settings) -> NotionProvider:
    return DirectNotionProvider(settings.notion_token.get_secret_value(), settings.notion_version)


def _default_llm(settings: Settings) -> LLMClient:
    local = OllamaClient(
        settings.ollama_base_url, settings.llm_model,
        temperature=settings.llm_temperature, num_ctx=settings.llm_num_ctx,
        timeout_s=settings.llm_timeout_s, keep_alive=settings.llm_keep_alive,
    )
    if not uses_cloud(settings):
        return local
    claude = ClaudeClient(
        settings.anthropic_api_key.get_secret_value(), settings.claude_model,
        timeout_s=settings.claude_timeout_s,
    )
    return FallbackLLM(claude, local)


def uses_cloud(settings: Settings) -> bool:
    return settings.llm_cloud and bool(settings.anthropic_api_key.get_secret_value())


def _default_speech(settings: Settings) -> SpeechToText:
    return WhisperLocal(settings)


class Sweeper:
    """Runs an expired-session flush every `interval_s`, forever, until stopped. One asyncio task
    loops sequentially — sleep, one flush, sleep again — so two runs can never overlap without
    any extra locking of their own. Whatever a run raises is logged and swallowed here: the loop's
    only job is to keep ticking, never to decide whether one sweep succeeded (that is already
    `Orchestrator.flush_expired_sessions`'s own job — this is a second, independent net under it,
    since main.py's caller must never depend on the orchestrator never changing that behaviour)."""

    def __init__(self, flush: Callable[[], Awaitable[object]], interval_s: float) -> None:
        self._flush = flush
        self._interval = interval_s
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._flush()
            except Exception:
                log.exception("session sweep failed")


@dataclass
class App:
    """Every object the process needs, constructed exactly once by `build()`. Plan 3b's tests
    build one with fake provider/llm factories and call `startup_checks`/`post_init_checks`/its
    collaborators directly — nothing in this module ever polls Telegram on its own."""

    settings: Settings
    store: AuditStore
    sessions: SessionStore
    descriptions: Descriptions
    discovery: Discovery
    provider: NotionProvider
    llm: LLMClient
    executor: Executor
    speech: SpeechToText
    orchestrator: Orchestrator
    admin: AdminServer
    sweeper: Sweeper
    telegram_app: Application
    # Set by post_init_checks on a fatal Notion auth failure. sys.exit() cannot be used there —
    # PTB's own bootstrap catches SystemExit around the post_init call and treats it as a
    # graceful stop (see post_init_checks' docstring) — so this is how the failure survives long
    # enough for main() to act on it once run_polling() returns.
    fatal: tuple[int, str] | None = None


def build(
    settings: Settings, *,
    provider_factory: ProviderFactory = _default_provider,
    llm_factory: LLMFactory = _default_llm,
    speech_factory: SpeechFactory = _default_speech,
) -> App:
    """Constructs every collaborator once and wires them together. Never touches the network or
    the filesystem beyond opening the sqlite file and reading `targets.yaml` — `provider_factory`/
    `llm_factory`/`speech_factory` are the seam a test uses to hand back a fake instead of a real
    `DirectNotionProvider`/`OllamaClient`/`WhisperLocal`, so `build()`, `startup_checks()` and
    `post_init_checks()` can all be exercised with no real Notion, Ollama, Telegram or Whisper
    anywhere in reach."""
    store = AuditStore(settings.db_path)
    sessions = SessionStore(store)
    descriptions = Descriptions(settings.targets_file)
    note = WorkspaceNote(settings.targets_file.with_name("workspace_note.md"))
    provider = provider_factory(settings)
    discovery = Discovery(
        provider, descriptions, items_per_target=settings.items_per_target,
        ttl_s=settings.schema_cache_ttl_s, inbox_target_id=settings.inbox_target_id,
        workspace_root=True,
    )
    # Web research needs Claude; without it the model is never offered a web_query at all.
    researcher = (
        WebResearcher(settings.anthropic_api_key.get_secret_value(), settings.research_model,
                      max_searches=settings.research_max_searches)
        if uses_cloud(settings) else None
    )
    context_builder = ContextBuilder(settings.timezone, settings.items_per_target, note=note.load,
                                     web_research=researcher is not None)
    llm = llm_factory(settings)
    validator = SemanticValidator()
    policy = Policy(Thresholds.from_settings(settings))
    executor = Executor(provider, images=ImageHost(provider))
    orchestrator = Orchestrator(
        settings, discovery, context_builder, llm, validator, policy, executor, store, sessions,
        researcher=researcher,
    )
    speech = speech_factory(settings)
    admin = AdminServer(settings, discovery, descriptions, note)
    sweeper = Sweeper(orchestrator.flush_expired_sessions, settings.session_ttl_s / 3)

    token = settings.telegram_bot_token.get_secret_value() or _PLACEHOLDER_TOKEN
    telegram_app = Application.builder().token(token).build()
    register(telegram_app, orchestrator, settings, speech, discovery, store)

    # `app` is assigned below, after these closures are defined but before either is ever called
    # (PTB only calls post_init/post_shutdown once run_polling() — or a test — invokes them), so
    # the late-binding closure over the name `app` sees the fully constructed object every time.
    async def _post_init(application: Application) -> None:
        await post_init_checks(app, application)
        if app.fatal is None:
            sweeper.start()

    async def _post_shutdown(_: Application) -> None:
        # Sweeper first: it is the one thing that still touches the store on its own timer, so it
        # must be off before the store underneath it closes. Admin next, store last — nothing
        # after this reaches for either.
        await sweeper.stop()
        admin.stop()
        store.close()

    telegram_app.post_init = _post_init
    telegram_app.post_shutdown = _post_shutdown

    app = App(
        settings=settings, store=store, sessions=sessions, descriptions=descriptions,
        discovery=discovery, provider=provider, llm=llm, executor=executor, speech=speech,
        orchestrator=orchestrator, admin=admin, sweeper=sweeper, telegram_app=telegram_app,
    )
    return app


def _die(code: int, message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


def _error_field_names(e: Exception) -> list[str]:
    """The field names a pydantic ValidationError names, and nothing else. `str(e)` for a
    "missing" error embeds `input_value={...}` — the whole dict of settings that *were* supplied,
    secrets included (pydantic truncates the repr, so what would leak is partial, not complete,
    but a partial token is still a leaked token) — and `e.errors()[i]["input"]` carries the exact
    same dict, untruncated. Only `err["loc"]` (the field path) is ever safe to surface."""
    errors = getattr(e, "errors", None)
    if not callable(errors):
        return []
    try:
        return sorted({".".join(str(p) for p in err.get("loc", ())) for err in errors()})
    except Exception:
        return []


def _config_error_message(e: Exception) -> str:
    """A fixed, token-safe message for a `Settings()` construction failure — never `str(e)`, for
    the reason `_error_field_names` explains."""
    fields = _error_field_names(e)
    where = f" ({', '.join(fields)})" if fields else ""
    return f"configuration error{where}. Fix .env and restart."


def startup_checks(app: App) -> None:
    """The two checks that can (and must) run before any event loop exists: settings
    (`Settings.require_telegram()`, exit 2 — only field *names* ever appear, via
    `_config_error_message`/`_error_field_names`, never a value) and schema
    (`AuditStore.assert_schema_current()`, exit 4, whose message already carries the "run the
    updater" hint). Neither is async. Giving them a throwaway event loop of their own anyway
    would be harmless in isolation, but see `post_init_checks` for why the checks that *are*
    async must not get one."""
    try:
        app.settings.require_telegram()
    except ValueError as e:
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    try:
        app.store.assert_schema_current()
    except MigrationError as e:
        _die(EXIT_PENDING_MIGRATION, str(e))


async def _register_commands(bot: Bot) -> None:
    """Publishes the command menu Telegram shows behind the "/" button. Without this call the
    client offers no menu at all and the user has to remember every command unaided.

    Never fatal: an unreachable Telegram here means the same thing it means anywhere else in this
    function — try again later — and the bot is perfectly usable with commands typed by hand. The
    log line carries the exception's *class* only, never its message: an `InvalidToken` raised
    here would carry the bot token in its text (see `_TELEGRAM_TOKEN_REJECTED`)."""
    commands = [BotCommand(name, description) for name, description in texts.COMMANDS.items()]
    try:
        await bot.set_my_commands(commands)
    except Exception as e:
        log.warning("could not publish the Telegram command menu (%s)", type(e).__name__)


async def post_init_checks(app: App, application: Application) -> None:
    """Everything that needs a running event loop: Ollama, Notion, initial discovery, the
    Telegram command menu, the admin server. Called from `Application.post_init` (wired in
    `build()`), so it shares PTB's own loop with every real call `app.provider`/`app.llm` make
    later — see the module docstring for why that is not optional.

    A Notion auth failure is the one fatal condition in here, but it cannot be reported with
    `sys.exit()`: `Application._Application__run` wraps the whole bootstrap — including the
    `post_init` call — in `except (KeyboardInterrupt, SystemExit): ...` and treats either as a
    plain graceful-stop signal, logs it at DEBUG, and lets `run_polling()` return normally with no
    trace of *why*. Recording `app.fatal` and calling `application.stop_running()` (PTB's own
    documented hook for "a graceful early shutdown ... if some condition is met [in post_init]")
    is what actually stops polling from starting; `main()` checks `app.fatal` once `run_polling()`
    returns and exits with the right code only then, once PTB's own shutdown/post_shutdown have
    already run cleanly."""
    if uses_cloud(app.settings):
        log.info("interpreter: %s, falling back to local %s",
                 app.settings.claude_model, app.settings.llm_model)
    else:
        log.info("interpreter: local %s", app.settings.llm_model)
    try:
        models = await app.llm.models()
    except LLMError as e:
        log.warning("ollama check failed (%s); continuing without it", e)
    else:
        if app.settings.llm_model not in models:
            log.warning(
                "configured LLM_MODEL %r not found on Ollama; run: ollama pull %s",
                app.settings.llm_model, app.settings.llm_model,
            )

    try:
        await app.provider.me()
    except NotionError as e:
        if e.status not in _NOTION_AUTH_STATUSES:
            log.warning("notion check failed (%s); continuing, discovery may still fail", e)
        else:
            app.fatal = (
                EXIT_NOTION_AUTH,
                f"Notion authentication failed ({e}). Check NOTION_TOKEN in .env.",
            )
            application.stop_running()
            return

    try:
        snapshot = await app.discovery.get()
    except Exception as e:  # same class of failure the orchestrator itself degrades on
        log.warning("initial discovery failed: %s", e)
    else:
        log.info("discovery: %d targets", len(snapshot.targets))
        inbox = next((t for t in snapshot.targets if t.is_inbox), None)
        if inbox is not None:
            log.info("inbox target: %s", inbox.name)
        else:
            log.warning("no inbox target flagged; the inbox fallback is inert")

    # Whisper (app.speech) is deliberately not touched here: the model loads lazily, off the
    # event loop, on the first voice message (app/speech/whisper_local.py).

    await _register_commands(application.bot)

    if app.settings.admin_ui_port > 0:
        # The admin page is optional by design (ADMIN_UI_PORT=0 turns it off), so losing the port
        # — already taken, or inside a Windows excluded port range, which is a routine WinError
        # 10013 on 8787 — must not take the bot down with it. Unguarded, this OSError would
        # escape post_init, sail past Application.__run's `except (KeyboardInterrupt,
        # SystemExit)` and out of run_polling().
        try:
            app.admin.start()
        except OSError as e:
            log.warning(
                "admin page could not bind 127.0.0.1:%d (%s); continuing without it",
                app.settings.admin_ui_port, e,
            )


def main() -> None:
    try:
        settings = load_settings()
    except Exception as e:  # pydantic ValidationError: a required .env value is missing/invalid
        configure("INFO")
        _die(EXIT_CONFIG, _config_error_message(e))

    # Logging is configured before build() on purpose: build() constructs the ExtBot that logs
    # the token-bearing API URL at DEBUG, so the floor and the redaction must already be in
    # place. The two secret *values* go in here and nowhere else.
    try:
        configure(
            settings.log_level,
            redact=(
                settings.telegram_bot_token.get_secret_value(),
                settings.notion_token.get_secret_value(),
                settings.anthropic_api_key.get_secret_value(),
            ),
            log_file=settings.log_file or None,
        )
    except ValueError as e:
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    # One bot per database: the lock sits next to the file it protects. Taken before build(),
    # which opens that database. `_die` raises SystemExit, so the `finally` still releases it.
    lock = InstanceLock(settings.db_path.parent / "bot.pid")
    try:
        lock.acquire()
    except AlreadyRunning:
        _die(EXIT_ALREADY_RUNNING, _ALREADY_RUNNING)
    try:
        app = build(settings)
        startup_checks(app)  # sync: settings + schema; exits 2/4 directly, no event loop involved
        try:
            app.telegram_app.run_polling()
        except InvalidToken:
            # Telegram refused the token (mistyped, revoked, regenerated). PTB re-raises this out
            # of run_polling() — it is neither KeyboardInterrupt nor SystemExit, so without this
            # catch Python would print the whole traceback, token and all. Nothing about `e` is
            # printed or logged: see _TELEGRAM_TOKEN_REJECTED.
            _die(EXIT_CONFIG, _TELEGRAM_TOKEN_REJECTED)
        if app.fatal is not None:  # post_init_checks found a fatal Notion auth failure, stopped
            _die(*app.fatal)
    finally:
        lock.release()


if __name__ == "__main__":
    main()
