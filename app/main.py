"""Application wiring, startup checks, and the process entry point.

Everything else in this codebase is a class waiting to be constructed; this module is where that
finally happens, once, for the single long-running process a user starts on their own machine
(`deploy/run.ps1`: `python -m app.main`). `build()` constructs every collaborator and returns them
on one `App`; `startup_checks()` runs the fixed sequence documentation/ERRORS.md specifies
(settings -> schema -> Ollama -> Notion -> initial discovery -> admin — step 8, polling, is
`main()`'s own job, not this function's); `main()` is the thin synchronous entry point that ties
`configure`/`build`/`startup_checks` to python-telegram-bot's own polling loop.

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

from telegram.ext import Application

from app.admin.server import AdminServer
from app.audit.migrate import MigrationError
from app.audit.store import AuditStore
from app.commands.executor import Executor
from app.config import Settings, load_settings
from app.conversation.orchestrator import Orchestrator
from app.conversation.session import SessionStore
from app.llm.base import LLMClient, LLMError
from app.llm.context import ContextBuilder
from app.llm.ollama import OllamaClient
from app.logging_setup import configure
from app.notion.descriptions import Descriptions
from app.notion.direct import DirectNotionProvider
from app.notion.discovery import Discovery
from app.notion.errors import NotionError
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

# Non-empty so python-telegram-bot's Bot() constructor (which only ever checks truthiness, never
# format) accepts it when the real token is blank. Real validity is settings.require_telegram()'s
# job — the very first startup check, which exits the process long before polling would ever put
# this placeholder in front of Telegram.
_PLACEHOLDER_TOKEN = "0:MISSING-TELEGRAM-BOT-TOKEN"

ProviderFactory = Callable[[Settings], NotionProvider]
LLMFactory = Callable[[Settings], LLMClient]
SpeechFactory = Callable[[Settings], SpeechToText]


def _default_provider(settings: Settings) -> NotionProvider:
    return DirectNotionProvider(settings.notion_token.get_secret_value(), settings.notion_version)


def _default_llm(settings: Settings) -> LLMClient:
    return OllamaClient(
        settings.ollama_base_url, settings.llm_model,
        temperature=settings.llm_temperature, num_ctx=settings.llm_num_ctx,
        timeout_s=settings.llm_timeout_s,
    )


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
    build one with fake provider/llm factories and call `startup_checks`/its collaborators
    directly — nothing in this module ever polls Telegram on its own."""

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


def build(
    settings: Settings, *,
    provider_factory: ProviderFactory = _default_provider,
    llm_factory: LLMFactory = _default_llm,
    speech_factory: SpeechFactory = _default_speech,
) -> App:
    """Constructs every collaborator once and wires them together. Never touches the network or
    the filesystem beyond opening the sqlite file and reading `targets.yaml` — `provider_factory`/
    `llm_factory`/`speech_factory` are the seam a test uses to hand back a fake instead of a real
    `DirectNotionProvider`/`OllamaClient`/`WhisperLocal`, so `build()` and `startup_checks()` can
    both be exercised with no real Notion, Ollama, Telegram or Whisper anywhere in reach."""
    store = AuditStore(settings.db_path)
    sessions = SessionStore(store)
    descriptions = Descriptions(settings.targets_file)
    provider = provider_factory(settings)
    discovery = Discovery(
        provider, descriptions, items_per_target=settings.items_per_target,
        ttl_s=settings.schema_cache_ttl_s, inbox_target_id=settings.inbox_target_id,
    )
    context_builder = ContextBuilder(settings.timezone, settings.items_per_target)
    llm = llm_factory(settings)
    validator = SemanticValidator()
    policy = Policy(Thresholds.from_settings(settings))
    executor = Executor(provider)
    orchestrator = Orchestrator(
        settings, discovery, context_builder, llm, validator, policy, executor, store, sessions
    )
    speech = speech_factory(settings)
    admin = AdminServer(settings, discovery, descriptions)
    sweeper = Sweeper(orchestrator.flush_expired_sessions, settings.session_ttl_s / 3)

    token = settings.telegram_bot_token.get_secret_value() or _PLACEHOLDER_TOKEN
    telegram_app = Application.builder().token(token).build()
    register(telegram_app, orchestrator, settings, speech, discovery, store)

    async def _post_init(_: Application) -> None:
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

    return App(
        settings=settings, store=store, sessions=sessions, descriptions=descriptions,
        discovery=discovery, provider=provider, llm=llm, executor=executor, speech=speech,
        orchestrator=orchestrator, admin=admin, sweeper=sweeper, telegram_app=telegram_app,
    )


def _die(code: int, message: str) -> NoReturn:
    print(message, file=sys.stderr)
    sys.exit(code)


async def startup_checks(app: App) -> None:
    """documentation/ERRORS.md's fixed startup sequence, steps 1-7 in order; step 8 (polling) is
    `main()`'s own job, run only once this returns without exiting. Every exit here prints one
    English operator line to stderr with its remedy and calls `sys.exit` with the documented
    code (2 config, 3 Notion auth, 4 pending migration); nothing else in this function is fatal —
    an unusable Ollama or a failed initial discovery only warn, since the user may start Ollama
    or fix Notion access later and the bot should still come up meanwhile."""
    try:
        app.settings.require_telegram()
    except ValueError as e:
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    try:
        app.store.assert_schema_current()
    except MigrationError as e:
        _die(EXIT_PENDING_MIGRATION, str(e))

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
        _die(EXIT_NOTION_AUTH,
             f"Notion authentication failed ({e}). Check NOTION_TOKEN in .env.")

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

    if app.settings.admin_ui_port > 0:
        app.admin.start()


def main() -> None:
    try:
        settings = load_settings()
    except Exception as e:  # pydantic ValidationError: a required .env value is missing/invalid
        configure("INFO")
        _die(EXIT_CONFIG, f"configuration error: {e}. Fix .env and restart.")

    configure(settings.log_level)
    app = build(settings)
    asyncio.run(startup_checks(app))
    app.telegram_app.run_polling()


if __name__ == "__main__":
    main()
