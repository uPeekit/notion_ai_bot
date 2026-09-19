"""app.telegram.handlers: the thin PTB transport over Orchestrator. Every handler is dispatched
here the same way python-telegram-bot's own Application.process_update would — walking the real
registered handlers and letting the real filters (allowed_filter included) decide whether the
handler runs at all — but without ever calling Application.initialize()/process_update(), which
would call Bot.get_me() and touch the network. No test here imports faster_whisper, a real
Telegram Bot, or a real Orchestrator: FakeOrchestrator/FakeSpeech/FakeDiscovery/FakeStore stand
in, and every Update/Message/CallbackQuery is a real PTB object built offline."""

from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

from telegram import CallbackQuery, Chat, Message, MessageEntity, Update, User, Voice
from telegram.ext import Application, ApplicationBuilder, CommandHandler

from app import texts
from app.config import Settings
from app.conversation.reply import Button, Reply
from app.logging_setup import bind_event
from app.notion.snapshot import Target, WorkspaceSnapshot
from app.speech.base import SpeechEmpty, SpeechError
from app.telegram import handlers as h

UTC = datetime.UTC
CHAT_ID = 100
ALLOWED_USER = 1
DENIED_USER = 99
DUMMY_TOKEN = "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"


# ---- fakes -------------------------------------------------------------------------------------


class FakeOrchestrator:
    """Records every call it receives and returns one canned Reply. `order`, when given, is a
    shared list handlers/bots append their own name to, so a test can assert call order."""

    def __init__(self, reply: Reply | None = None, order: list[str] | None = None):
        self.calls: list[tuple] = []
        self.reply = reply if reply is not None else Reply("ok")
        self.order = order

    async def handle_text(self, chat_id, user_id, text, *, kind="text", transcript=None):
        if self.order is not None:
            self.order.append("handle_text")
        self.calls.append(("handle_text", chat_id, user_id, text, kind, transcript))
        return self.reply

    async def handle_callback(self, chat_id, user_id, data):
        if self.order is not None:
            self.order.append("orchestrator")
        self.calls.append(("handle_callback", chat_id, user_id, data))
        return self.reply

    async def undo(self, chat_id, execution_id=None):
        self.calls.append(("undo", chat_id, execution_id))
        return self.reply

    async def cancel(self, chat_id):
        self.calls.append(("cancel", chat_id))
        return self.reply


class FakeSpeech:
    def __init__(self, result: str | Exception = "transcribed text"):
        self.calls: list[Path] = []
        self.result = result

    async def transcribe(self, path: Path) -> str:
        self.calls.append(path)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


class FakeDiscovery:
    def __init__(self, snapshot: WorkspaceSnapshot | None = None, fail: Exception | None = None):
        self._snapshot = snapshot
        self.fail = fail
        self.invalidated = False
        self.last = snapshot

    def invalidate(self) -> None:
        self.invalidated = True

    async def get(self) -> WorkspaceSnapshot:
        if self.fail is not None:
            raise self.fail
        self.last = self._snapshot
        return self._snapshot


class FakeStore:
    def __init__(self, raise_on_record: Exception | None = None):
        self.recorded: list[tuple[int, int]] = []
        self.raise_on_record = raise_on_record

    def set_reply_message_id(self, execution_id: int, message_id: int) -> None:
        if self.raise_on_record is not None:
            raise self.raise_on_record
        self.recorded.append((execution_id, message_id))


@dataclass
class SentMessage:
    message_id: int


@dataclass
class FakeFile:
    content: bytes = b"fake-ogg-bytes"
    paths: list[Path] = field(default_factory=list)

    async def download_to_drive(self, path) -> None:
        p = Path(path)
        p.write_bytes(self.content)
        self.paths.append(p)


class FakeBot:
    """context.bot for every handler test. `username` is what CommandHandler needs to match a
    command's target (message.get_bot().username); `order`, when given, is the same shared list
    FakeOrchestrator appends to."""

    username = "notionbot"

    def __init__(self, order: list[str] | None = None):
        self.sent: list[dict] = []
        self.answered: list[str] = []
        self.order = order
        self.file = FakeFile()
        self._next_id = 1000

    async def send_message(self, chat_id, text, reply_markup=None, **kwargs):
        self._next_id += 1
        self.sent.append({
            "chat_id": chat_id, "text": text, "reply_markup": reply_markup,
            "message_id": self._next_id,
        })
        return SentMessage(self._next_id)

    async def answer_callback_query(self, callback_query_id, text=None, **kwargs):
        if self.order is not None:
            self.order.append("answered")
        self.answered.append(callback_query_id)
        return True

    async def get_file(self, file_id):
        return self.file

    async def send_chat_action(self, chat_id, action, **kwargs):
        if self.order is not None:
            self.order.append(f"action:{action}")

    edit_error: Exception | None = None

    async def edit_message_reply_markup(self, chat_id=None, message_id=None,
                                        inline_message_id=None, reply_markup=None, **kwargs):
        if self.edit_error is not None:
            raise self.edit_error
        if self.order is not None:
            self.order.append("keyboard_cleared")
        self.cleared = getattr(self, "cleared", []) + [(chat_id, message_id, reply_markup)]
        return True


# ---- Update/Message builders ---------------------------------------------------------------


def _chat() -> Chat:
    return Chat(id=CHAT_ID, type="private")


def _user(uid: int) -> User:
    return User(id=uid, is_bot=False, first_name="u")


def _bound(obj, bot):
    obj.set_bot(bot)
    return obj


def text_update(uid: int, text: str, bot: FakeBot, update_id: int = 1) -> Update:
    msg = _bound(
        Message(message_id=update_id, date=datetime.datetime.now(UTC), chat=_chat(),
               from_user=_user(uid), text=text),
        bot,
    )
    return Update(update_id=update_id, message=msg)


def command_update(uid: int, command: str, bot: FakeBot, update_id: int = 1) -> Update:
    text = f"/{command}"
    entity = MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(text))
    msg = _bound(
        Message(message_id=update_id, date=datetime.datetime.now(UTC), chat=_chat(),
               from_user=_user(uid), text=text, entities=[entity]),
        bot,
    )
    return Update(update_id=update_id, message=msg)


def voice_update(uid: int, bot: FakeBot, update_id: int = 1) -> Update:
    voice = Voice(file_id="voice-1", file_unique_id="vu-1", duration=3)
    msg = _bound(
        Message(message_id=update_id, date=datetime.datetime.now(UTC), chat=_chat(),
               from_user=_user(uid), voice=voice),
        bot,
    )
    return Update(update_id=update_id, message=msg)


def callback_update(uid: int, data: str, bot: FakeBot, update_id: int = 1) -> Update:
    msg = _bound(
        Message(message_id=update_id, date=datetime.datetime.now(UTC), chat=_chat(),
               from_user=_user(uid), text="q"),
        bot,
    )
    query = _bound(
        CallbackQuery(id="cbq-1", from_user=_user(uid), chat_instance="ci", data=data,
                     message=msg),
        bot,
    )
    return Update(update_id=update_id, callback_query=query)


# ---- harness -------------------------------------------------------------------------------


def make_settings() -> Settings:
    return Settings(
        _env_file=None, notion_token="ntn-test-token", telegram_bot_token="tg-test-token",
        telegram_allowed_user_ids=f"{ALLOWED_USER},2",
    )


def build_app() -> Application:
    return ApplicationBuilder().token(DUMMY_TOKEN).build()


async def dispatch(app: Application, update: Update, context) -> bool:
    """What Application.process_update does, minus persistence and the error-handler wrapping
    (error_handler is tested directly, not through this path) and minus Bot.initialize()'s
    get_me() network call — check_update/handle_update need neither."""
    for handler in app.handlers[0]:
        check = handler.check_update(update)
        if check not in (None, False):
            await handler.handle_update(update, app, check, context)
            return True
    return False


def sample_snapshot() -> WorkspaceSnapshot:
    shopping = Target(
        id="ds-shop", kind="database", name="Покупки", path="Покупки", description="",
        parent_page_id=None, database_id="db-shop", fields=[], items=[],
        operations=frozenset({"create"}), url="https://notion.so/shop", is_inbox=False,
    )
    misc = Target(
        id="pg-misc", kind="page", name="Разное", path="Разное", description="",
        parent_page_id=None, database_id=None, fields=[], items=[],
        operations=frozenset({"append"}), url="https://notion.so/misc", is_inbox=True,
    )
    return WorkspaceSnapshot(fetched_at=datetime.datetime.now(UTC), targets=[shopping, misc])


@dataclass
class Harness:
    app: Application
    orch: FakeOrchestrator
    speech: FakeSpeech
    discovery: FakeDiscovery
    store: FakeStore
    bot: FakeBot
    context: SimpleNamespace


def build(
    *, reply: Reply | None = None, speech_result: str | Exception = "transcribed text",
    snapshot: WorkspaceSnapshot | None = None, discovery_fail: Exception | None = None,
    order: list[str] | None = None, store: FakeStore | None = None,
) -> Harness:
    settings = make_settings()
    app = build_app()
    orch = FakeOrchestrator(reply=reply, order=order)
    speech = FakeSpeech(result=speech_result)
    discovery = FakeDiscovery(snapshot=snapshot, fail=discovery_fail)
    store = store if store is not None else FakeStore()
    h.register(app, orch, settings, speech, discovery, store)
    bot = FakeBot(order=order)
    context = SimpleNamespace(bot_data=app.bot_data, bot=bot, error=None)
    return Harness(app, orch, speech, discovery, store, bot, context)


# ---- text ----------------------------------------------------------------------------------


async def test_text_message_reaches_handle_text_with_right_chat_user_text():
    hs = build(reply=Reply("done", buttons=[[Button("a:tok:x", "X")]]))
    update = text_update(ALLOWED_USER, "купи молоко", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == [
        ("handle_text", CHAT_ID, ALLOWED_USER, "купи молоко", "text", None)
    ]
    assert len(hs.bot.sent) == 1
    sent = hs.bot.sent[0]
    assert sent["chat_id"] == CHAT_ID
    assert sent["text"] == "done"
    assert sent["reply_markup"] is not None
    assert sent["reply_markup"].inline_keyboard[0][0].callback_data == "a:tok:x"


async def test_unauthorised_text_produces_no_send_and_no_orchestrator_call():
    hs = build()
    update = text_update(DENIED_USER, "купи молоко", hs.bot)

    dispatched = await dispatch(hs.app, update, hs.context)

    assert dispatched is False
    assert hs.orch.calls == []
    assert hs.bot.sent == []


async def test_unauthorised_text_logs_auth_denied_exactly_once(caplog):
    """The declarative gate (allowed_filter) must log the denial itself — nothing in this
    module's handlers checks authorisation for a text message, so without logging inside the
    filter this path would be denied silently."""
    hs = build()
    update = text_update(DENIED_USER, "купи молоко", hs.bot)

    with caplog.at_level(logging.WARNING):
        dispatched = await dispatch(hs.app, update, hs.context)

    assert dispatched is False
    assert hs.orch.calls == []
    assert hs.bot.sent == []
    denied = [r for r in caplog.records if "AUTH_DENIED" in r.getMessage()]
    assert len(denied) == 1
    assert str(DENIED_USER) in denied[0].getMessage()


async def test_unauthorised_command_logs_auth_denied_exactly_once(caplog):
    hs = build()
    update = command_update(DENIED_USER, "undo", hs.bot)

    with caplog.at_level(logging.WARNING):
        dispatched = await dispatch(hs.app, update, hs.context)

    assert dispatched is False
    assert hs.orch.calls == []
    assert hs.bot.sent == []
    denied = [r for r in caplog.records if "AUTH_DENIED" in r.getMessage()]
    assert len(denied) == 1
    assert str(DENIED_USER) in denied[0].getMessage()


async def test_allowed_text_emits_no_auth_denied_record(caplog):
    hs = build()
    update = text_update(ALLOWED_USER, "купи молоко", hs.bot)

    with caplog.at_level(logging.WARNING):
        assert await dispatch(hs.app, update, hs.context)

    assert not any("AUTH_DENIED" in r.getMessage() for r in caplog.records)


# ---- voice -----------------------------------------------------------------------------------


async def test_voice_note_transcribes_and_deletes_temp_file():
    hs = build(speech_result="купи молоко")
    update = voice_update(ALLOWED_USER, hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == [
        ("handle_text", CHAT_ID, ALLOWED_USER, "купи молоко", "voice", "купи молоко")
    ]
    # the "transcribing" notice first, then the orchestrator's own reply
    assert [m["text"] for m in hs.bot.sent] == [texts.VOICE_TRANSCRIBING, "ok"]
    downloaded = hs.bot.file.paths[0]
    assert not downloaded.exists()  # deleted in the handler's finally


async def test_speech_empty_replies_and_never_calls_orchestrator():
    hs = build(speech_result=SpeechEmpty())
    update = voice_update(ALLOWED_USER, hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == []
    assert len(hs.bot.sent) == 2  # the notice, then the failure message
    assert "распозна" in hs.bot.sent[1]["text"].lower() or "голосовое" in hs.bot.sent[1]["text"]
    assert not hs.bot.file.paths[0].exists()


async def test_speech_error_replies_and_never_calls_orchestrator():
    hs = build(speech_result=SpeechError("boom"))
    update = voice_update(ALLOWED_USER, hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == []
    assert len(hs.bot.sent) == 2  # the notice, then the failure message
    assert hs.bot.sent[1]["text"] == texts.ERRORS["STT_FAILED"]
    assert not hs.bot.file.paths[0].exists()


async def test_unauthorised_voice_produces_no_send():
    hs = build()
    update = voice_update(DENIED_USER, hs.bot)

    dispatched = await dispatch(hs.app, update, hs.context)

    assert dispatched is False
    assert hs.speech.calls == []
    assert hs.orch.calls == []
    assert hs.bot.sent == []


# ---- callback query ----------------------------------------------------------------------------


async def test_callback_answered_before_orchestrator_called_then_dispatched():
    order: list[str] = []
    hs = build(reply=Reply("answered"), order=order)
    update = callback_update(ALLOWED_USER, "a:tok:opt1", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert order == ["answered", "orchestrator", "keyboard_cleared"]
    assert hs.bot.answered == ["cbq-1"]
    assert hs.orch.calls == [("handle_callback", CHAT_ID, ALLOWED_USER, "a:tok:opt1")]
    assert len(hs.bot.sent) == 1


async def test_unauthorised_callback_produces_no_send_and_no_answer():
    hs = build()
    update = callback_update(DENIED_USER, "a:tok:opt1", hs.bot)

    await dispatch(hs.app, update, hs.context)  # CallbackQueryHandler has no filter: always True

    assert hs.bot.answered == []
    assert hs.orch.calls == []
    assert hs.bot.sent == []


# ---- /undo, /cancel, /start, /help --------------------------------------------------------


async def test_undo_command_hits_orchestrator_undo():
    hs = build(reply=Reply("undone"))
    update = command_update(ALLOWED_USER, "undo", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == [("undo", CHAT_ID, None)]
    assert hs.bot.sent[0]["text"] == "undone"


async def test_cancel_command_hits_orchestrator_cancel():
    hs = build(reply=Reply("cancelled"))
    update = command_update(ALLOWED_USER, "cancel", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == [("cancel", CHAT_ID)]
    assert hs.bot.sent[0]["text"] == "cancelled"


async def test_start_command_replies_with_help_text_and_touches_no_orchestrator():
    hs = build()
    update = command_update(ALLOWED_USER, "start", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.orch.calls == []
    assert hs.bot.sent[0]["text"] == texts.HELP_TEXT


async def test_help_command_replies_with_help_text():
    hs = build()
    update = command_update(ALLOWED_USER, "help", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.bot.sent[0]["text"] == texts.HELP_TEXT


# ---- /refresh, /targets ----------------------------------------------------------------------


async def test_refresh_command_invalidates_and_reports_target_count():
    hs = build(snapshot=sample_snapshot())
    update = command_update(ALLOWED_USER, "refresh", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.discovery.invalidated is True
    assert hs.bot.sent[0]["text"] == texts.REFRESH_DONE.format(count=2)


async def test_refresh_command_reports_discovery_failure():
    from app.notion.errors import NotionError

    hs = build(discovery_fail=NotionError(500, "internal_server_error", "boom"))
    update = command_update(ALLOWED_USER, "refresh", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.bot.sent[0]["text"] == texts.ERRORS["DISCOVERY_FAILED"]


async def test_targets_command_renders_tree_with_inbox_marker():
    # FakeDiscovery(snapshot=...) seeds `.last` directly, as a prior discovery would have —
    # /targets must render from it without calling `.get()` (no fetch on this path).
    hs = build(snapshot=sample_snapshot())
    update = command_update(ALLOWED_USER, "targets", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    text = hs.bot.sent[0]["text"]
    assert "Покупки" in text
    assert "Разное" in text
    assert "(разное)" in text


async def test_targets_command_with_no_snapshot_yet_says_so_without_fetching():
    hs = build(snapshot=None)
    update = command_update(ALLOWED_USER, "targets", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.bot.sent[0]["text"] == texts.TARGETS_NONE_YET


# ---- undo_id -> set_reply_message_id --------------------------------------------------------


async def test_reply_with_undo_id_records_sent_message_id():
    hs = build(reply=Reply("done", undo_id=42))
    update = text_update(ALLOWED_USER, "hi", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert len(hs.store.recorded) == 1
    execution_id, message_id = hs.store.recorded[0]
    assert execution_id == 42
    assert message_id == hs.bot.sent[0]["message_id"]


async def test_set_reply_message_id_failure_does_not_break_the_reply():
    store = FakeStore(raise_on_record=RuntimeError("db locked"))
    hs = build(reply=Reply("done", undo_id=42), store=store)
    update = text_update(ALLOWED_USER, "hi", hs.bot)

    assert await dispatch(hs.app, update, hs.context)  # must not raise

    assert len(hs.bot.sent) == 1
    assert hs.bot.sent[0]["text"] == "done"


# ---- error_handler -----------------------------------------------------------------------------


async def test_error_handler_replies_once_and_swallows_the_exception():
    bot = FakeBot()
    update = text_update(ALLOWED_USER, "hi", bot)
    context = SimpleNamespace(bot=bot, error=RuntimeError("boom"), bot_data={})

    await h.error_handler(update, context)  # must not raise

    assert len(bot.sent) == 1
    assert bot.sent[0]["text"] == texts.ERRORS["INTERNAL"]


async def test_error_handler_with_no_known_chat_does_not_send():
    bot = FakeBot()
    context = SimpleNamespace(bot=bot, error=RuntimeError("boom"), bot_data={})

    await h.error_handler(object(), context)  # not even an Update

    assert bot.sent == []


async def test_voice_notice_is_sent_before_transcription_begins():
    """The first voice note of a run loads the Whisper model — and the very first one ever
    downloads ~1.5 GB of it — with nothing visible in Telegram meanwhile. Without a notice sent
    *before* speech.transcribe is awaited, a working bot and a dead one look identical."""
    order: list[str] = []
    hs = build()

    real_transcribe = hs.speech.transcribe

    async def recording_transcribe(path):
        order.append("transcribe")
        return await real_transcribe(path)

    hs.speech.transcribe = recording_transcribe
    real_send = hs.bot.send_message

    async def recording_send(chat_id, text, **kwargs):
        order.append("send:" + text)
        return await real_send(chat_id, text, **kwargs)

    hs.bot.send_message = recording_send

    assert await dispatch(hs.app, voice_update(ALLOWED_USER, hs.bot), hs.context)

    assert order[0] == "send:" + texts.VOICE_TRANSCRIBING
    assert order[1] == "transcribe"


async def test_error_handler_logs_the_event_id_of_the_turn_in_flight(caplog):
    """`getattr(context, "event_id", None)` was always None — nothing in python-telegram-bot ever
    sets that attribute. The id lives in the same contextvar the log filter reads."""
    bot = FakeBot()
    update = text_update(ALLOWED_USER, "hi", bot)
    context = SimpleNamespace(bot=bot, error=RuntimeError("boom"), bot_data={})

    with caplog.at_level(logging.ERROR, logger="app.telegram.handlers"):
        with bind_event(77):
            await h.error_handler(update, context)

    assert any("event_id=77" in r.getMessage() for r in caplog.records)


def test_every_registered_command_is_in_the_published_menu_and_the_help_text():
    """Telegram's "/" menu comes from texts.COMMANDS; a command registered here but missing from
    it is invisible to the user, and one missing from HELP_TEXT is undocumented."""
    app = build_app()
    h.register(app, FakeOrchestrator(), make_settings(), FakeSpeech(), FakeDiscovery(), FakeStore())

    registered = {
        name
        for handler in app.handlers[0]
        if isinstance(handler, CommandHandler)
        for name in handler.commands
    }

    assert registered == set(texts.COMMANDS)
    for name in registered:
        assert f"/{name}" in texts.HELP_TEXT


async def test_answered_question_loses_its_keyboard_before_the_reply():
    """A reply such as «Отменено.» under a question still showing its buttons reads as nothing
    having happened — which is exactly how the first real cancel was reported."""
    order: list[str] = []
    hs = build(reply=Reply("Отменено."), order=order)
    update = callback_update(ALLOWED_USER, "a:tok:cancel", hs.bot, update_id=77)

    assert await dispatch(hs.app, update, hs.context)

    assert hs.bot.cleared == [(CHAT_ID, 77, None)]  # that message, keyboard removed
    assert order == ["answered", "orchestrator", "keyboard_cleared"]
    assert [m["text"] for m in hs.bot.sent] == ["Отменено."]


async def test_a_keyboard_telegram_will_not_edit_does_not_cost_the_reply():
    from telegram.error import BadRequest

    hs = build(reply=Reply("ok"))
    hs.bot.edit_error = BadRequest("Message can't be edited")
    update = callback_update(ALLOWED_USER, "a:tok:opt1", hs.bot)

    assert await dispatch(hs.app, update, hs.context)

    assert [m["text"] for m in hs.bot.sent] == ["ok"]


async def test_polling_network_errors_log_one_line_a_minute_without_a_traceback(
    caplog, monkeypatch
):
    """Offline (DNS fails, Wi-Fi drops) PTB's polling raises NetworkError every ~2 s and retries
    by itself; each used to log as an "unhandled exception" with a 100-line traceback."""
    from telegram.error import NetworkError

    now = [1000.0]
    monkeypatch.setattr(h.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(h, "_last_network_warning", float("-inf"))
    bot = FakeBot()
    context = SimpleNamespace(bot=bot, error=NetworkError("httpx.ConnectError: getaddrinfo failed"),
                              bot_data={})
    with caplog.at_level(logging.WARNING, logger="app.telegram.handlers"):
        for _ in range(10):
            await h.error_handler(None, context)
            now[0] += 2
        now[0] += 60
        await h.error_handler(None, context)
    records = [r for r in caplog.records if r.name == "app.telegram.handlers"]
    assert len(records) == 2
    assert all(r.levelno == logging.WARNING and r.exc_info is None for r in records)
    assert "getaddrinfo failed" in records[0].getMessage()
    assert bot.sent == []


async def test_a_network_error_inside_a_turn_is_still_an_error(caplog):
    from telegram.error import NetworkError

    bot = FakeBot()
    update = text_update(ALLOWED_USER, "hi", bot)
    context = SimpleNamespace(bot=bot, error=NetworkError("boom"), bot_data={})
    with caplog.at_level(logging.ERROR, logger="app.telegram.handlers"):
        await h.error_handler(update, context)
    assert any(r.levelno == logging.ERROR for r in caplog.records)


async def test_targets_command_marks_hidden_targets():
    from dataclasses import replace

    snap = sample_snapshot()
    snap = replace(snap, targets=[replace(t, hidden=t.id == "ds-shop") for t in snap.targets])
    hs = build(snapshot=snap)
    assert await dispatch(hs.app, command_update(ALLOWED_USER, "targets", hs.bot), hs.context)
    buy = next(line for line in hs.bot.sent[0]["text"].splitlines() if "Покупки" in line)
    assert texts.TARGETS_HIDDEN_MARKER in buy


async def test_typing_is_shown_while_a_message_is_handled():
    order: list[str] = []
    hs = build(order=order)
    assert await dispatch(hs.app, text_update(ALLOWED_USER, "купи молоко", hs.bot), hs.context)
    assert order.index("action:typing") < order.index("handle_text")
