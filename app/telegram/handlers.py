"""The thin PTB transport over app.conversation.orchestrator.Orchestrator: parse one update, call
one orchestrator method, render the Reply it returns, stop. No Interpretation, no Notion payload,
no decision about what gets saved lives here — that is the orchestrator's job, and if a handler
below starts making one, that is a bug in this module, not a missing feature.

`register` is the only export most callers need; it wires every handler onto the PTB
`Application` and stashes the five collaborators (orchestrator, settings, speech, discovery,
audit store) on `application.bot_data`, so every handler function below reads them from
`context.bot_data` rather than closing over them — which keeps the handler functions themselves
plain, module-level, and callable directly from a test with a hand-built `context`.

Every handler except the callback-query one is gated declaratively: `allowed_filter(settings)`
is ANDed into the `MessageHandler`/`CommandHandler` filters at registration, so PTB's own
dispatch never invokes the handler at all for an unauthorised update — see app.telegram.auth.
`CallbackQueryHandler` has no `filters=` parameter in python-telegram-bot, so `_on_callback` is
the one handler that checks `is_allowed` itself, as the very first thing it does and before any
Telegram call (no `answer()`, no send) — a structural exception forced by PTB's API, not a
relaxation of the rule.

The orchestrator never raises to its caller (a Plan 3a invariant), so an exception surfacing out
of a handler body here means a bug in this transport layer, not in the pipeline — handlers must
not paper over that with a broad `except`; `error_handler` is PTB's global hook and the one place
that catches everything.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import tempfile
import time
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from app import texts
from app.audit.store import AuditStore
from app.config import Settings
from app.conversation.orchestrator import Orchestrator
from app.conversation.reply import Reply
from app.logging_setup import event_id_var
from app.notion.discovery import Discovery
from app.notion.snapshot import WorkspaceSnapshot
from app.speech.base import SpeechEmpty, SpeechError, SpeechToText
from app.telegram.auth import allowed_filter, deny, is_allowed
from app.telegram.keyboards import to_markup

log = logging.getLogger(__name__)

# app.bot_data keys register() populates and every handler below reads from context.bot_data.
_ORCH = "orch"
_SETTINGS = "settings"
_SPEECH = "speech"
_DISCOVERY = "discovery"
_STORE = "store"


def register(
    app: Application, orch: Orchestrator, settings: Settings, speech: SpeechToText,
    discovery: Discovery, store: AuditStore,
) -> None:
    app.bot_data[_ORCH] = orch
    app.bot_data[_SETTINGS] = settings
    app.bot_data[_SPEECH] = speech
    app.bot_data[_DISCOVERY] = discovery
    app.bot_data[_STORE] = store

    gate = allowed_filter(settings)
    app.add_handler(CommandHandler(["start", "help"], _cmd_help, filters=gate))
    app.add_handler(CommandHandler("undo", _cmd_undo, filters=gate))
    app.add_handler(CommandHandler("cancel", _cmd_cancel, filters=gate))
    app.add_handler(CommandHandler("refresh", _cmd_refresh, filters=gate))
    app.add_handler(CommandHandler("targets", _cmd_targets, filters=gate))
    # `gate` is the outermost (rightmost) operand of every `&` below, not the leftmost: PTB's
    # `_MergedFilter.filter` always evaluates its `base_filter` first and short-circuits the
    # `and_filter` when that base is falsy, so putting the cheap type check first means `gate`
    # — and the AUTH_DENIED it logs — only runs for the one handler a given update could
    # possibly match, never for the other MessageHandler's chain too. `filters=gate` on the
    # CommandHandlers above needs no such ordering: CommandHandler.check_update already resolves
    # the command name before it ever consults `self.filters`.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & gate, _on_text))
    app.add_handler(
        MessageHandler((filters.VOICE | filters.AUDIO | filters.VIDEO_NOTE) & gate, _on_voice)
    )
    app.add_handler(CallbackQueryHandler(_on_callback))
    app.add_error_handler(error_handler)


# ---- sending -------------------------------------------------------------------------------


async def _send(
    update: Update, context: ContextTypes.DEFAULT_TYPE, reply: Reply, store: AuditStore
) -> None:
    """Sends reply.text with reply.buttons rendered by to_markup, no parse_mode: Plan 3a's text
    is plain and may contain punctuation Markdown would mangle. When the reply carries an
    undo_id, the sent message's id is recorded against that execution so a later turn can find
    the message to edit; a failure to record it must not take back a reply already on its way
    to the user, so it is only logged."""
    chat_id = update.effective_chat.id
    sent = await context.bot.send_message(
        chat_id=chat_id, text=reply.text, reply_markup=to_markup(reply)
    )
    if reply.undo_id is not None:
        try:
            store.set_reply_message_id(reply.undo_id, sent.message_id)
        except Exception:
            log.exception("failed to record reply message id for execution %s", reply.undo_id)


# ---- text / voice ----------------------------------------------------------------------------

TYPING_EVERY_S = 4.0  # Telegram shows "typing…" for ~5 s per chat action


@contextlib.asynccontextmanager
async def _typing(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    """"typing…" in the chat for as long as the block runs: a message usually takes a few
    seconds, a web search up to a minute, and silence reads as a dead bot. Best effort — a
    failed chat action never costs the reply."""

    async def typing() -> None:
        try:
            await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
        except Exception:  # noqa: BLE001 -- cosmetic; the reply matters, not this
            pass

    async def keep_typing() -> None:
        while True:
            await asyncio.sleep(TYPING_EVERY_S)
            await typing()

    await typing()  # at once, not after the first sleep: most turns end within seconds
    task = asyncio.create_task(keep_typing())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orch: Orchestrator = context.bot_data[_ORCH]
    async with _typing(context, update.effective_chat.id):
        reply = await orch.handle_text(
            update.effective_chat.id, update.effective_user.id, update.message.text
        )
    await _send(update, context, reply, context.bot_data[_STORE])


async def _on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    speech: SpeechToText = context.bot_data[_SPEECH]
    store: AuditStore = context.bot_data[_STORE]
    message = update.message
    media = message.voice or message.audio or message.video_note
    # Sent before anything slow happens. The first voice note of a run loads the Whisper model —
    # and the very first one ever downloads it too, ~1.5 GB, with nothing visible in Telegram —
    # so without this the user has no way to tell a working bot from a dead one.
    await _send(update, context, Reply(texts.VOICE_TRANSCRIBING), store)
    file = await context.bot.get_file(media.file_id)

    fd, tmp_name = tempfile.mkstemp(suffix=".ogg")
    os.close(fd)
    tmp_path = Path(tmp_name)
    try:
        await file.download_to_drive(tmp_path)
        transcript = await speech.transcribe(tmp_path)
    except SpeechEmpty:
        await _send(update, context, Reply(texts.ERRORS["STT_EMPTY"]), store)
        return
    except SpeechError:
        await _send(update, context, Reply(texts.ERRORS["STT_FAILED"]), store)
        return
    finally:
        tmp_path.unlink(missing_ok=True)

    orch: Orchestrator = context.bot_data[_ORCH]
    async with _typing(context, update.effective_chat.id):
        reply = await orch.handle_text(
            update.effective_chat.id, update.effective_user.id, transcript,
            kind="voice", transcript=transcript,
        )
    await _send(update, context, reply, store)


# ---- callback query ----------------------------------------------------------------------------


def _guard_callback(update: Update, settings: Settings) -> int | None:
    """The one handler PTB cannot gate declaratively (CallbackQueryHandler takes no `filters=`):
    the sender's id is checked here, first, before `answer()` or anything else Telegram-facing
    runs. Every other handler below trusts allowed_filter entirely and carries no such check."""
    query = update.callback_query
    user_id = query.from_user.id if query is not None and query.from_user else None
    if is_allowed(user_id, settings.allowed_user_ids):
        return user_id
    deny(user_id, update.effective_chat.id if update.effective_chat else None)
    return None


async def _on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings: Settings = context.bot_data[_SETTINGS]
    user_id = _guard_callback(update, settings)
    if user_id is None:
        return
    query = update.callback_query
    await query.answer()  # Telegram's 10-second budget; never carries text
    orch: Orchestrator = context.bot_data[_ORCH]
    reply = await orch.handle_callback(update.effective_chat.id, user_id, query.data)
    await _clear_keyboard(query)
    await _send(update, context, reply, context.bot_data[_STORE])


async def _clear_keyboard(query) -> None:
    """The pressed button's question is dealt with: take its keyboard away, so it visibly
    resolves (a bare cancel confirmation under a question still showing its buttons reads as
    nothing having happened) and a stale button cannot be pressed again. If the orchestrator
    needs to ask again, its reply carries a fresh keyboard. Best effort: Telegram refuses to edit a
    message older than 48 hours, and that is no reason to lose the reply itself."""
    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except TelegramError as e:
        log.debug("could not clear the answered keyboard: %s", type(e).__name__)


# ---- commands ----------------------------------------------------------------------------------


async def _cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _send(update, context, Reply(texts.HELP_TEXT), context.bot_data[_STORE])


async def _cmd_undo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orch: Orchestrator = context.bot_data[_ORCH]
    reply = await orch.undo(update.effective_chat.id)
    await _send(update, context, reply, context.bot_data[_STORE])


async def _cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orch: Orchestrator = context.bot_data[_ORCH]
    reply = await orch.cancel(update.effective_chat.id)
    await _send(update, context, reply, context.bot_data[_STORE])


async def _cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    discovery: Discovery = context.bot_data[_DISCOVERY]
    store: AuditStore = context.bot_data[_STORE]
    discovery.invalidate()
    try:
        snapshot = await discovery.get()
    except Exception as e:  # same class of failure app.conversation.orchestrator degrades on
        log.warning("refresh failed: %s", e)
        await _send(update, context, Reply(texts.ERRORS["DISCOVERY_FAILED"]), store)
        return
    reply = Reply(texts.REFRESH_DONE.format(count=len(snapshot.targets)))
    await _send(update, context, reply, store)


async def _cmd_targets(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    discovery: Discovery = context.bot_data[_DISCOVERY]
    store: AuditStore = context.bot_data[_STORE]
    snapshot = discovery.last
    if snapshot is None:
        await _send(update, context, Reply(texts.TARGETS_NONE_YET), store)
        return
    await _send(update, context, Reply(_targets_text(snapshot)), store)


def _targets_text(snapshot: WorkspaceSnapshot) -> str:
    """One line per target: its full path (the breadcrumb already ends in the target's own
    name, so the path alone carries both), its kind, and texts.TARGETS_INBOX_MARKER on whichever
    one is the inbox — the point of /targets is to show the user at a glance where an unresolved
    message goes."""
    lines = [texts.TARGETS_HEADER]
    for t in snapshot.targets:
        marker = f" {texts.TARGETS_INBOX_MARKER}" if t.is_inbox else ""
        if t.hidden:
            marker += f" {texts.TARGETS_HIDDEN_MARKER}"
        lines.append(f"{t.path} — {texts.TARGET_KIND_LABELS[t.kind]}{marker}")
    return "\n".join(lines)


# ---- errors --------------------------------------------------------------------------------


# Polling hits NetworkError every couple of seconds while the machine is offline (Wi-Fi drop,
# sleep); PTB retries on its own and resumes when the network is back. One line a minute says so.
NETWORK_WARNING_INTERVAL_S = 60.0
_last_network_warning = float("-inf")


def _log_network_blip(error: BaseException) -> None:
    global _last_network_warning
    now = time.monotonic()
    if now - _last_network_warning >= NETWORK_WARNING_INTERVAL_S:
        _last_network_warning = now
        log.warning("Telegram unreachable (%s); retrying until the network is back", error)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """PTB's global error hook: the one place in this module a broad exception is caught, by
    design (see module docstring). Never re-raises. The audit `event_id` comes from
    `logging_setup.event_id_var` — the same contextvar the root filter stamps onto every record —
    so an exception raised while a turn is in flight names the row it belongs to, and one raised
    outside any turn logs `-`."""
    if update is None and isinstance(context.error, NetworkError):
        _log_network_blip(context.error)  # polling, not a turn: nobody to tell, nothing lost
        return
    event_id = event_id_var.get()
    log.error("unhandled exception (event_id=%s)", event_id, exc_info=context.error)
    chat = update.effective_chat if isinstance(update, Update) else None
    if chat is None:
        return
    try:
        await context.bot.send_message(chat_id=chat.id, text=texts.ERRORS["INTERNAL"])
    except Exception:
        log.exception("failed to notify chat %s of an internal error", chat.id)
