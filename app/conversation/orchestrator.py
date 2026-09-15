"""The conversation orchestrator: the one entry point Plan 3b's Telegram handlers will call.

It joins the pieces every other module contributes — discovery, context, schema, LLM, semantic
validator, policy, command builder, executor, audit, sessions, resolver, inbox — into the four
things a user can do: send a message, press a button, undo, cancel. Everything it hands back is
an app.conversation.reply.Reply; nothing here imports python-telegram-bot, and no Russian
literal lives here (app/texts.py owns every string the user reads, and the LLM-facing `pending`
keys too).

Turns of one chat are serialised by a per-chat asyncio.Lock taken at every entry point: a
double-tapped button delivers two callbacks into two concurrent handlers, and both would pass a
read-then-write guard (`events.executed`, `executions.undone`, a session save) that is only safe
against itself. Different chats stay fully concurrent.

Three rules hold on every path through this module:

  * **One audit row per handled message.** `_open` inserts the `events` row, every stage writes
    its columns onto the in-memory `_Turn`, and `_close` flushes them once — including on the
    error exits, so no path can leave a half-written row behind.
  * **It never raises to its caller.** Every failure becomes a Reply carrying a message from
    `texts.ERRORS` plus an audited error code; `_guard` catches whatever nobody expected.
  * **A button answer never calls the LLM.** `apply_answer` + `result_from_session` rebuild the
    interpretation from the stored session (Task 3), so a clarification round-trip costs one
    Notion snapshot and nothing else.

Callback payloads are `a:<token>:<option_id>` (an answer), `u:<execution_id>` (undo) and
`i:<event_id>` (save that message's own text to the inbox — the BTN_INBOX offer attached to a
reply that saved nothing, where there is no question and therefore no session token to answer).

The inbox is the safety net, not a shortcut: it catches messages the pipeline would otherwise
drop (LLM down, rejected interpretation, a question the user never answered), never a question
the bot could have asked, and never when Notion itself is unreachable — there would be nothing
to write to.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from app import texts
from app.audit.store import AuditStore
from app.commands.builder import build_command
from app.commands.executor import ExecutionResult, Executor, UndoRecord
from app.commands.models import Search
from app.config import Settings
from app.conversation.inbox import inbox_command, inbox_target
from app.conversation.reply import Button, Reply, format_execution, format_question, format_search
from app.conversation.resolver import apply_answer, next_question, options_for, result_from_session
from app.conversation.session import (
    MAX_QUESTIONS,
    PendingSession,
    SessionStore,
    session_from_decision,
)
from app.interpretation.models import Interpretation
from app.llm.base import (
    LLMClient,
    LLMContextOverflow,
    LLMInvalidOutput,
    LLMTrace,
    LLMUnavailable,
)
from app.llm.context import Context, ContextBuilder
from app.llm.output_schema import build_schema
from app.notion.discovery import Discovery
from app.notion.errors import NotionError
from app.notion.snapshot import Target, WorkspaceSnapshot
from app.validation.policy import Decision, Policy
from app.validation.semantic import SemanticValidator, ValidationResult

log = logging.getLogger(__name__)

# Option ids format_question renders on its own (the type-specific extras plus the trailing
# cancel/inbox row). resolver.options_for returns them mixed in with the real answers, so they
# are filtered out before the option list is handed over, or they would appear twice.
RESERVED_OPTIONS = frozenset({"cancel", "inbox", "confirm", "other", "add_new"})
GENERIC_REJECT = "INTENT_UNKNOWN"
# The placeholder each validator-issued REJECT template asks for, and which Issue.detail fills.
REJECT_DETAIL: dict[str, str] = {"SEM_TYPE": "field_name", "SEM_UNSUPPORTED_OP": "target_name"}
# Decision placeholder written when an events row is opened; every exit overwrites it.
OPEN = "OPEN"
# A free-text answer is folded into the request it answers ("original\nanswer") and the result
# becomes the next round's original_text, so a user who keeps answering in free text grows it
# without bound — MAX_QUESTIONS only counts button answers. Same ceiling the validator and the
# inbox put on a single piece of text; without it a stuck loop ends in LLMContextOverflow.
MAX_PROMPT = 4000
# /undo and /cancel arrive without the sender's id (see Orchestrator.undo/cancel), and so does
# the expired-session sweeper; the events row still needs a non-null user column.
NO_USER = 0


def now_utc() -> datetime:
    return datetime.now(UTC)


def _kind(kind: str) -> str:
    return json.dumps({"kind": kind})


def _error(code: str, **fmt: Any) -> str:
    """ERRORS[code] filled in, degrading to the generic INTENT_UNKNOWN message when the code is
    unknown or the caller could not supply every placeholder (NOTION_4XX takes a message,
    item_not_found an item_text, UNDO_EXPIRED a minute count)."""
    try:
        return texts.ERRORS[code].format(**fmt)
    except (KeyError, IndexError):
        return texts.ERRORS[GENERIC_REJECT]


def _reject_code(decision: Decision) -> str:
    """A REJECT from the validator carries "<CODE>: <message>" reasons; the two rejects that
    carry a candidate and a question instead (item_not_found, nothing_to_write) name their code
    through that question, whose type is itself a key of texts.ERRORS."""
    if decision.questions and decision.questions[0].type in texts.ERRORS:
        return decision.questions[0].type
    code = decision.reasons[0].split(":", 1)[0] if decision.reasons else ""
    return code if code in texts.ERRORS else GENERIC_REJECT


def _reject_fmt(decision: Decision, result: ValidationResult) -> dict[str, Any]:
    """What ERRORS[_reject_code(decision)] needs to be filled in. The two rejects that carry a
    candidate name it themselves; a validator-issued REJECT has none, so its placeholder comes
    from the Issue that produced the code (Issue.detail: the field name behind SEM_TYPE, the
    target name behind SEM_UNSUPPORTED_OP). Without this both templates could only ever degrade
    to the generic INTENT_UNKNOWN message."""
    if decision.candidate is not None:
        return {"item_text": decision.candidate.item_text or texts.UNTITLED,
                "target_name": decision.candidate.target.name}
    code = _reject_code(decision)
    key = REJECT_DETAIL.get(code)
    if key is None:
        return {}
    detail = next((i.detail for i in result.issues if i.code == code and i.detail), None)
    return {key: detail} if detail is not None else {}


def _undo_buttons(execution_id: int | None) -> list[list[Button]]:
    if execution_id is None:
        return []
    return [[Button(f"u:{execution_id}", texts.BTN_UNDO)]]


def _prefixed(reply: Reply, prefix: str) -> Reply:
    return reply if not prefix else replace(reply, text=f"{prefix}\n{reply.text}")


@dataclass
class _Turn:
    """One handled message: its open `events` row, the columns to write back when it closes, and
    the single clock reading every expiry inside the turn is measured against."""

    event_id: int
    chat_id: int
    now: datetime
    # What this row's raw_input holds, when that is the user's own words: the only text an
    # `i:<event_id>` offer on this turn could faithfully replay.
    source_text: str | None = None
    started: float = field(default_factory=monotonic)
    cols: dict[str, Any] = field(default_factory=dict)
    # An error code to record only if the turn's own work records none of its own. The rescue of
    # an *expired* session runs before the new message has even been interpreted, so it cannot
    # know yet whether this row will end up carrying a more informative code than INBOX_FAILED.
    fallback_error: str | None = None

    def audit(self, **cols: Any) -> None:
        self.cols.update(cols)

    def audit_fallback(self, code: str) -> None:
        self.fallback_error = code


class Orchestrator:
    def __init__(
        self, settings: Settings, discovery: Discovery, builder: ContextBuilder, llm: LLMClient,
        validator: SemanticValidator, policy: Policy, executor: Executor, store: AuditStore,
        sessions: SessionStore, clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._s = settings
        self._discovery = discovery
        self._builder = builder
        self._llm = llm
        self._validator = validator
        self._policy = policy
        self._executor = executor
        self._store = store
        self._sessions = sessions
        self._clock = clock
        # One lock per chat with a turn in flight, dropped again when the last one leaves, so a
        # long-lived process does not accumulate one per chat it has ever seen.
        self._locks: dict[int, asyncio.Lock] = {}
        self._waiting: dict[int, int] = {}

    @asynccontextmanager
    async def _chat_lock(self, chat_id: int) -> AsyncIterator[None]:
        """Serialise one chat's turns. Every guard in this module is a read, an await, then a
        write — `events.executed` before an inbox save, `executions.undone` before an undo, the
        session row across a save — and Telegram delivers a double-tapped button as two
        callbacks ~100 ms apart into two concurrent handlers, where both reads see the state
        from before either write. Other chats are unaffected: the lock is per chat_id."""
        lock = self._locks.get(chat_id)
        if lock is None:
            lock = self._locks[chat_id] = asyncio.Lock()
        self._waiting[chat_id] = self._waiting.get(chat_id, 0) + 1
        try:
            async with lock:
                yield
        finally:
            remaining = self._waiting[chat_id] - 1
            if remaining:
                self._waiting[chat_id] = remaining
            else:
                del self._waiting[chat_id]
                del self._locks[chat_id]

    # ---- entry points ------------------------------------------------------------------------

    async def handle_text(
        self, chat_id: int, user_id: int, text: str, *, kind: str = "text",
        transcript: str | None = None,
    ) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, kind, lambda t: self._text(t, text),
                                    raw_input=text, transcription=transcript)

    async def handle_callback(self, chat_id: int, user_id: int, data: str) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, "callback",
                                    lambda t: self._callback(t, data), raw_input=data)

    async def undo(self, chat_id: int, execution_id: int | None = None) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, NO_USER, "undo",
                                    lambda t: self._undo(t, execution_id))

    async def cancel(self, chat_id: int) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, NO_USER, "cancel", self._cancel)

    async def flush_expired_sessions(self) -> int:
        """Sweeper: rescue the text behind every question nobody answered before its TTL ran out.
        Plan 3b schedules it; a message from the same chat rescues its own expired session on the
        way past (see `_text`), so this only catches chats that went quiet."""
        flushed = 0
        try:
            now = self._clock()
            if not self._store.expired_sessions(now):
                return 0
            try:
                await self._discovery.get()
            except Exception as e:  # nothing to write to: keep the sessions for a later sweep
                log.warning("sweep skipped, discovery failed: %s", e)
                return 0
            # One chat at a time, each under its own lock and never one lock for the whole
            # sweep. The pop belongs inside it: a turn that is already holding this chat's
            # session would otherwise have it rescued into the inbox underneath it, and the
            # message would land twice.
            for chat_id, _ in self._store.expired_sessions(now):
                async with self._chat_lock(chat_id):
                    session = self._sessions.pop_expired_one(chat_id, now)
                    if session is None:  # answered, renewed or swept since the scan
                        continue
                    await self._turn(chat_id, NO_USER, "sweep",
                                     lambda t, s=session: self._sweep(t, s))
                flushed += 1
        except Exception:  # runs on a timer, not behind a chat handler: report what it managed
            log.exception("session sweep aborted")
        return flushed

    async def _sweep(self, turn: _Turn, session: PendingSession) -> Reply:
        """One expired session. The Reply goes nowhere — the sweeper has no chat to answer — but
        routing it through `_turn` is what gives each rescue its own closed audit row."""
        turn.audit(decision=_kind("SWEEP"))
        target = await self._inbox_target(forced=False)
        if target is not None and await self._rescue(
            turn, session.original_text, target, note=self._question_note(session)
        ) is None:
            turn.audit(error="INBOX_FAILED")
        return Reply("")

    # ---- text pipeline -----------------------------------------------------------------------

    async def _text(self, turn: _Turn, text: str) -> Reply:
        try:
            snapshot = await self._discovery.get()
        except Exception as e:  # any transport failure reads the same to the user
            log.warning("discovery failed: %s", e)
            return self._plain(turn, "DISCOVERY_FAILED")

        # Ask about expiry before `get`, which drops an expired row without telling anyone.
        expired = self._sessions.pop_expired_one(turn.chat_id, turn.now)
        prefix = await self._expired_prefix(turn, expired) if expired is not None else ""
        session = None if expired is not None else self._sessions.get(turn.chat_id, turn.now)

        # A live session makes this message a free-text answer: the model sees the question it is
        # answering and both halves of the request, and its fresh interpretation replaces the
        # session outright (an unrelated message is simply a new request, F5).
        pending = self._pending_block(session, snapshot) if session is not None else None
        prompt = (f"{session.original_text}\n{text}"[:MAX_PROMPT] if session is not None
                  else text)
        asked = list(session.asked) if session is not None else []

        ctx = self._builder.build(snapshot, turn.now, pending)
        if not ctx.target_keys():
            return _prefixed(self._plain(turn, "DISCOVERY_FAILED"), prefix)
        turn.audit(llm_context=ctx.json())
        try:
            interp, trace = await self._llm.interpret(prompt, ctx, build_schema(ctx))
        except (LLMUnavailable, LLMInvalidOutput, LLMContextOverflow) as e:
            code = "LLM_UNAVAILABLE" if isinstance(e, LLMUnavailable) else "LLM_INVALID_OUTPUT"
            log.warning("llm failed (%s): %s", code, e)
            return _prefixed(await self._inbox_or_error(turn, prompt, code), prefix)

        self._audit_llm(turn, interp, trace)
        result = self._validator.validate(interp, ctx, snapshot)
        decision = self._policy.evaluate(result)
        self._audit_result(turn, result, decision)
        return _prefixed(await self._dispatch(turn, decision, result, ctx, prompt, asked), prefix)

    async def _dispatch(
        self, turn: _Turn, decision: Decision, result: ValidationResult, ctx: Context, text: str,
        asked: list[str],
    ) -> Reply:
        """Step 7 of the pipeline, shared by a fresh interpretation and a resumed session."""
        if decision.kind == "EXECUTE":
            self._sessions.drop(turn.chat_id)
            return await self._execute(turn, decision, result, text)
        # CLARIFY, plus the two REJECTs that carry a candidate and a question: item_not_found is
        # actionable through its BTN_ADD_NEW button, nothing_to_write through free text.
        if decision.candidate is not None and decision.questions:
            question = await self._ask(turn, decision, result, ctx, text, asked)
            if question is not None:
                return question
        self._sessions.drop(turn.chat_id)
        return await self._inbox_or_error(turn, text, _reject_code(decision),
                                          **_reject_fmt(decision, result))

    async def _execute(
        self, turn: _Turn, decision: Decision, result: ValidationResult, text: str
    ) -> Reply:
        candidate = decision.candidate
        assert candidate is not None
        command = build_command(candidate, result.intent, text)
        turn.audit(command=command.model_dump_json())
        try:
            executed = await self._executor.run(command)
        except NotionError as e:
            log.warning("execute failed: %s", e)
            code = "NOTION_4XX" if 400 <= e.status < 500 else "NOTION_5XX"
            # The write did not happen, so the text would be lost otherwise.
            return await self._inbox_or_error(turn, text, code, message=e.message)
        turn.audit(executed=1, notion_page_id=executed.page_id)
        if isinstance(command, Search):
            return Reply(format_search(executed))
        execution_id = self._record_execution(turn, executed)
        return Reply(format_execution(executed, target_url=candidate.target.url),
                     _undo_buttons(execution_id), undo_id=execution_id)

    async def _ask(
        self, turn: _Turn, decision: Decision, result: ValidationResult, ctx: Context, text: str,
        asked: list[str],
    ) -> Reply | None:
        """Store the next unanswered question as this chat's pending session and render it.
        None when nothing is left to ask or the session has spent its MAX_QUESTIONS budget — the
        caller then falls back to the inbox rather than interrogating the user forever."""
        question = next_question(decision, asked, ctx)
        if question is None or len(asked) >= MAX_QUESTIONS:
            return None
        candidate = decision.candidate
        assert candidate is not None
        options = options_for(question, candidate, result, ctx)
        session = session_from_decision(
            turn.chat_id, turn.event_id, text, result, replace(decision, questions=[question]),
            options, now=turn.now, ttl_s=self._s.session_ttl_s, asked=asked,
        )
        self._sessions.save(session)
        turn.audit(clarification_state=session.model_dump_json())
        return format_question(
            question, [(o.id, o.label) for o in options if o.id not in RESERVED_OPTIONS],
            session.token, inbox=await self._inbox_target(forced=True) is not None,
            target_name=candidate.target.name,
        )

    def _target_name(self, session: PendingSession) -> str | None:
        """The pending question's target, named from the cached snapshot. Used where the caller
        has no snapshot of its own (a callback runs no discovery): None simply falls back to the
        target-less wording of the question."""
        return self._named(session, self._discovery.last)

    @staticmethod
    def _named(session: PendingSession, snapshot: WorkspaceSnapshot | None) -> str | None:
        best = session.best
        if snapshot is None or best is None:
            return None
        target = snapshot.target(best.target_id)
        return target.name if target is not None else None

    @staticmethod
    def _question_text(session: PendingSession, target_name: str | None) -> str:
        return format_question(session.question, [], session.token, inbox=False,
                               target_name=target_name).text

    def _question_note(self, session: PendingSession) -> str:
        """Why a rescued message is in the inbox rather than in its target: nobody answered
        this. Written next to the text so the user can triage the page later."""
        return texts.INBOX_NOTE_UNANSWERED.format(
            question=self._question_text(session, self._target_name(session)))

    def _pending_block(self, session: PendingSession, snapshot: WorkspaceSnapshot) -> dict:
        """What the model is told about the question already on screen: its text, the target's
        name and the original request. Names only — a Notion id or a context key here would be
        echoed back as if the model had chosen it."""
        name = self._named(session, snapshot)
        return {
            texts.PENDING_QUESTION: self._question_text(session, name),
            texts.PENDING_TARGET: name or "",
            texts.PENDING_TEXT: session.original_text,
        }

    # ---- callbacks ---------------------------------------------------------------------------

    async def _callback(self, turn: _Turn, data: str) -> Reply:
        prefix, _, rest = data.partition(":")
        if prefix == "u" and rest.isdigit():
            return await self._undo(turn, int(rest))
        if prefix == "i" and rest.isdigit():
            return await self._inbox_event(turn, int(rest))
        token, _, option_id = rest.partition(":")
        if prefix != "a" or not option_id:
            log.warning("unknown callback payload in event %s", turn.event_id)
            return self._plain(turn, "SESSION_EXPIRED")
        session = self._sessions.get(turn.chat_id, turn.now)
        if session is None or session.token != token:
            # A stale token belongs to a question that has already been answered or replaced;
            # applying its answer to the current session would act on the wrong request.
            return self._plain(turn, "SESSION_EXPIRED")

        answered, verb = apply_answer(session, option_id)
        if verb == "cancel":
            self._sessions.drop(turn.chat_id)
            turn.audit(decision=_kind("CANCEL"))
            return Reply(texts.CANCELLED)
        if verb == "inbox":
            turn.audit(decision=_kind("INBOX"))
            reply, saved = await self._to_inbox(turn, session.original_text, None, forced=True,
                                                note=self._question_note(session))
            if saved and reply is not None:
                self._sessions.drop(turn.chat_id)
                return reply
            # The write failed, or the inbox disappeared between rendering the button and
            # pressing it. The session is the only copy of the text, so it stays, and the
            # question comes back with its own keyboard: its BTN_INBOX button is the retry.
            turn.audit(error="INBOX_FAILED")
            failed = reply.text if reply is not None else _error("SESSION_EXPIRED")
            return self._resend(session, failed, self._target_name(session))
        if verb == "free_text":
            turn.audit(decision=_kind("FREE_TEXT"))
            return Reply(texts.ENTER_VALUE)  # the session stays: the next message answers it
        return await self._resume(turn, answered)

    @staticmethod
    def _resend(session: PendingSession, text: str, target_name: str | None) -> Reply:
        """The pending question's keyboard again under a different line of text, for when a
        button could not do what it promised: the question is still open and every one of its
        answers — a second attempt at BTN_INBOX included — is still valid under the same token.
        The question itself comes back under the failure line — a keyboard with no question
        over it asks the user to answer something the chat has scrolled past — and `target_name`
        is passed on so it reads the same as it did the first time: texts.QUESTION_WITH_TARGET,
        which names the target, rather than the bare texts.QUESTION."""
        keyboard = format_question(
            session.question,
            [(o.id, o.label) for o in session.options if o.id not in RESERVED_OPTIONS],
            session.token, inbox=True, target_name=target_name,
        )
        return replace(keyboard, text=f"{text}\n{keyboard.text}")

    async def _resume(self, turn: _Turn, session: PendingSession) -> Reply:
        """Re-evaluate an answered session against a fresh snapshot — no second LLM call. The
        rebuild goes through Notion ids, so it survives a rediscovery that renumbered every key."""
        try:
            snapshot = await self._discovery.get()
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return self._plain(turn, "DISCOVERY_FAILED")
        ctx = self._builder.build(snapshot, turn.now)
        result = result_from_session(session, snapshot, ctx)
        decision = self._policy.evaluate(result)
        self._audit_result(turn, result, decision)
        return await self._dispatch(turn, decision, result, ctx, session.original_text,
                                    list(session.asked))

    async def _undo(self, turn: _Turn, execution_id: int | None) -> Reply:
        minutes = max(1, self._s.undo_window_s // 60)
        row = (self._store.get_execution(execution_id, turn.now) if execution_id is not None
               else self._store.latest_execution(turn.chat_id, turn.now))
        if row is None or row["undone"] or row["chat_id"] != turn.chat_id:
            return self._plain(turn, "UNDO_EXPIRED", minutes=minutes)
        try:
            await self._executor.undo(UndoRecord.model_validate_json(row["undo"]))
        except NotionError as e:
            log.warning("undo failed: %s", e)
            return self._plain(turn, "UNDO_FAILED", message=e.message)
        self._store.mark_undone(row["id"])
        turn.audit(decision=_kind("UNDO"))
        return Reply(texts.UNDONE)

    async def _cancel(self, turn: _Turn) -> Reply:
        self._sessions.drop(turn.chat_id)
        turn.audit(decision=_kind("CANCEL"))
        return Reply(texts.CANCELLED)

    # ---- inbox fallback ----------------------------------------------------------------------

    async def _inbox_target(self, *, forced: bool) -> Target | None:
        """The target to fall back to, or None when the caller should reply with the plain error.
        `forced` is a button press: mode `button` means "only when I ask for it", so it saves
        then and only then; mode `off` means never.

        It fetches its own snapshot rather than trusting an earlier step to have populated
        `discovery.last`: the callback paths never run discovery, and after a restart inside
        SESSION_TTL_SECONDS there is no cached snapshot for a button press to find."""
        mode = self._s.inbox_mode
        if mode == "off" or (mode == "button" and not forced):
            return None
        try:
            snapshot: WorkspaceSnapshot | None = await self._discovery.get()
        except Exception as e:
            log.warning("discovery failed, using the cached snapshot for the inbox: %s", e)
            snapshot = self._discovery.last
        return inbox_target(snapshot) if snapshot is not None else None

    async def _to_inbox(
        self, turn: _Turn, text: str, code: str | None, *, forced: bool = False,
        note: str | None = None, **fmt: Any
    ) -> tuple[Reply | None, bool]:
        """Persist `text` to the flagged inbox and report it. Returns (reply, saved): a reply of
        None means there was no inbox to write to at all and the caller should say something
        else; `saved` is False when the write itself failed, which is what decides whether the
        caller offers the BTN_INBOX button. Direct: no LLM, no validator, no policy, and no
        second fallback — a failed write degrades to INBOX_FAILED rather than retrying.

        `note` is the short line filed next to the text saying why it is in the inbox; a caller
        that knows better (an unanswered question) passes its own, and an error code renders its
        own user-facing message as the reason."""
        target = await self._inbox_target(forced=forced)
        if target is None:
            return None, False
        reason = _error(code, **fmt) if code else None
        prefix = f"{reason} " if reason else ""
        if note is None and reason is not None:
            note = texts.INBOX_NOTE.format(reason=reason)
        written = await self._rescue(turn, text, target, note=note)
        if written is None:
            return Reply(prefix + texts.INBOX_FAILED.format(target_name=target.name)), False
        result, execution_id = written
        done = texts.INBOX_SAVED.format(target_name=target.name, url=result.url or target.url)
        return Reply(prefix + done, _undo_buttons(execution_id), undo_id=execution_id), True

    async def _inbox_or_error(self, turn: _Turn, text: str, code: str, **fmt: Any) -> Reply:
        """The error exits all end here: save what the user said if the inbox is on, and offer
        to save it otherwise (or when the save failed) rather than dropping the message."""
        turn.audit(error=code, decision=json.dumps({"kind": "ERROR", "code": code}))
        reply, saved = await self._to_inbox(turn, text, code, **fmt)
        if saved:
            return reply if reply is not None else Reply(_error(code, **fmt))
        return replace(reply if reply is not None else Reply(_error(code, **fmt)),
                       buttons=await self._inbox_offer(turn, text))

    async def _inbox_offer(self, turn: _Turn, text: str) -> list[list[Button]]:
        """The BTN_INBOX offer on a reply that saved nothing. It carries this event's id rather
        than a session token because a rejected message has no question and so no session;
        pressing it replays that row's own `raw_input` through `_inbox_event`. Which is why it is
        only offered when this turn's row does hold exactly `text`: a callback row's raw_input is
        the button payload, and a free-text answer's is only half of the request."""
        if turn.source_text != text or await self._inbox_target(forced=True) is None:
            return []
        return [[Button(f"i:{turn.event_id}", texts.BTN_INBOX)]]

    async def _inbox_event(self, turn: _Turn, event_id: int) -> Reply:
        """Answer to the BTN_INBOX offer: save that event's own text now. Refused for another
        chat's event, or one older than a pending question would have been allowed to live
        (SESSION_TTL_SECONDS) — the button is a prompt reply, not an open-ended handle on the
        audit log."""
        row = self._store.get_event(event_id)
        if row is None or row["chat_id"] != turn.chat_id:
            return self._plain(turn, "SESSION_EXPIRED")
        text = (row["raw_input"] or row["transcription"] or "").strip()
        age = turn.now - datetime.fromisoformat(row["ts"])
        if not text or age > timedelta(seconds=self._s.session_ttl_s):
            return self._plain(turn, "SESSION_EXPIRED")
        turn.audit(decision=_kind("INBOX"))
        if row["executed"]:
            # The keyboard on a delivered message cannot be taken away, so the guard lives here:
            # pressing the offer twice must not mint a second copy in Notion.
            return Reply(texts.INBOX_ALREADY_SAVED)
        # The offer only exists because that row failed; its code is the reason worth filing.
        note = (texts.INBOX_NOTE.format(reason=_error(row["error"])) if row["error"] else None)
        reply, saved = await self._to_inbox(turn, text, None, forced=True, note=note)
        if saved:
            # Mark the *source* row, not this one: that message's text is what reached Notion,
            # and it is what the next press of the same button has to be refused against.
            self._store.update_event(event_id, executed=1)
        # No inbox any more: repeat what the original reply said instead of saving.
        return reply if reply is not None else Reply(_error(row["error"] or GENERIC_REJECT))

    async def _rescue(
        self, turn: _Turn, text: str, target: Target | None = None, *, note: str | None = None
    ) -> tuple[ExecutionResult, int | None] | None:
        """Run the inbox command, returning None when it could not be written. A ValueError from
        inbox_command (empty text, or a database inbox with no title property) is a configuration
        problem, not a transport one, and degrades the same way rather than raising."""
        target = target if target is not None else await self._inbox_target(forced=False)
        if target is None:
            return None
        try:
            command = inbox_command(target, text, note=note, now=turn.now,
                                    tz=self._s.timezone)
            result = await self._executor.run(command)
        except (NotionError, ValueError) as e:
            log.warning("inbox write failed: %s", e)
            return None
        return result, self._record_execution(turn, result)

    async def _expired_prefix(self, turn: _Turn, expired: PendingSession) -> str:
        """A question nobody answered in time: its text goes to the inbox and the user is told
        so, on top of whatever the message they just sent produces.

        A failed rescue is audited, unlike its `_inbox_or_error` sibling, which leaves the row
        holding the original failure code. Here the row belongs to a *different* message — the
        one the user just sent, which may well succeed — so without this the rescued text would
        be gone with no trace of it anywhere in the audit log, which is the one thing the inbox
        exists to prevent. It is a fallback: a code the turn records for itself wins."""
        target = await self._inbox_target(forced=False)
        if target is None:
            return ""
        written = await self._rescue(turn, expired.original_text, target,
                                     note=self._question_note(expired))
        if written is None:
            turn.audit_fallback("INBOX_FAILED")
        template = texts.INBOX_SAVED_EXPIRED if written is not None else texts.INBOX_FAILED
        return template.format(target_name=target.name)

    # ---- audit -------------------------------------------------------------------------------

    async def _turn(
        self, chat_id: int, user_id: int, kind: str,
        work: Callable[[_Turn], Awaitable[Reply]], **cols: Any,
    ) -> Reply:
        """Open the events row, run `work`, close the row — with all three inside the guard. The
        audit store is the one dependency that cannot report its own failure through the audit
        log, so a locked or unwritable database degrades to a logged INTERNAL reply instead of
        reaching the transport as an exception."""
        try:
            turn = self._open(chat_id, user_id, kind, **cols)
        except Exception:
            log.exception("could not open an audit event for chat %s", chat_id)
            return Reply(_error("INTERNAL"))
        try:
            reply = await work(turn)
        except Exception:  # the caller is a chat handler: it gets a Reply, always
            log.exception("unhandled failure in event %s", turn.event_id)
            reply = self._plain(turn, "INTERNAL")
        try:
            self._finish(turn)
        except Exception:  # the answer is already earned; losing the audit row must not eat it
            log.exception("could not close audit event %s", turn.event_id)
        return reply

    def _open(self, chat_id: int, user_id: int, kind: str, **cols: Any) -> _Turn:
        event_id = self._store.new_event(
            telegram_user_id=user_id, chat_id=chat_id, kind=kind,
            **{k: v for k, v in cols.items() if v is not None},
        )
        return _Turn(
            event_id, chat_id, self._clock(), source_text=cols.get("raw_input"),
            # A placeholder no exit ever writes, so the column is never null and a row that was
            # closed without a decision is visible as exactly that.
            cols={"decision": _kind(OPEN)},
        )

    def _finish(self, turn: _Turn) -> None:
        if turn.fallback_error and not turn.cols.get("error"):
            turn.audit(error=turn.fallback_error)
        self._store.update_event(
            turn.event_id, duration_ms=int((monotonic() - turn.started) * 1000), **turn.cols)

    def _plain(self, turn: _Turn, code: str, **fmt: Any) -> Reply:
        turn.audit(error=code, decision=json.dumps({"kind": "ERROR", "code": code}))
        return Reply(_error(code, **fmt))

    def _record_execution(self, turn: _Turn, result: ExecutionResult) -> int | None:
        """Executions are rows only when they can be reverted; `reply_message_id` is filled in by
        Plan 3b after the reply is actually sent (there is no message id at this point)."""
        if result.undo is None:
            return None
        return self._store.add_execution(
            turn.event_id, turn.chat_id, None, result.undo.model_dump_json(),
            turn.now + timedelta(seconds=self._s.undo_window_s),
        )

    @staticmethod
    def _audit_llm(turn: _Turn, interp: Interpretation, trace: LLMTrace) -> None:
        turn.audit(
            llm_model=trace.model,
            # The events table has no token columns, so the counts ride along with the response
            # they describe. trace.messages (the prompt) is already in llm_context.
            llm_response=json.dumps({
                "raw": trace.raw_response, "attempts": trace.attempts,
                "duration_ms": trace.duration_ms, "prompt_tokens": trace.prompt_tokens,
                "output_tokens": trace.output_tokens, "done_reason": trace.done_reason,
            }, ensure_ascii=False),
            interpretation=interp.model_dump_json(),
        )

    @staticmethod
    def _audit_result(turn: _Turn, result: ValidationResult, decision: Decision) -> None:
        turn.audit(
            candidate_scores=json.dumps(
                [{"target": c.target.name, "confidence": c.confidence}
                 for c in result.candidates], ensure_ascii=False),
            validation_result=json.dumps({
                "intent": result.intent, "intent_confidence": result.intent_confidence,
                "issues": [{"code": i.code, "message": i.message, "key": i.key}
                           for i in result.issues],
            }, ensure_ascii=False),
            decision=json.dumps({
                "kind": decision.kind, "risk": decision.risk,
                "questions": [q.type for q in decision.questions], "reasons": decision.reasons,
            }, ensure_ascii=False),
        )
