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
from difflib import SequenceMatcher
from time import monotonic
from typing import Any

from pydantic import ValidationError

from app import address, texts
from app.audit.store import AuditStore
from app.commands.builder import build_command
from app.commands.executor import ExecutionResult, Executor, Refused, UndoRecord
from app.commands.models import Command, CreateItem, CreatePage, Search
from app.config import Settings
from app.conversation.inbox import inbox_command, inbox_target
from app.conversation.plan import PlanState, PlanStep, StepSpec
from app.conversation.reply import Button, Reply, format_execution, format_question, format_search
from app.conversation.resolver import apply_answer, next_question, options_for, result_from_session
from app.conversation.session import (
    MAX_QUESTIONS,
    PendingSession,
    SessionStore,
    session_from_decision,
)
from app.conversation.steps import to_interpretation
from app.interpretation.models import Interpretation
from app.llm.base import (
    LLMClient,
    LLMContextOverflow,
    LLMInvalidOutput,
    LLMTrace,
    LLMUnavailable,
)
from app.llm.context import Context, ContextBuilder
from app.llm.health import Health
from app.llm.output_schema import build_schema
from app.llm.planner import PlanError, Planner, Verdict
from app.llm.prompts import WEB_WORDS, plan_context, workspace_summary
from app.llm.research import (
    ResearchError,
    ResearchQuestion,
    ResearchTimeout,
    WebResearcher,
)
from app.logging_setup import bind_event
from app.notion import stats as notion_stats
from app.notion import titles
from app.notion.discovery import Discovery
from app.notion.errors import NotionError
from app.notion.snapshot import Target, WorkspaceSnapshot
from app.switches import Switches
from app.tuning import Tuning
from app.validation.policy import Decision, Policy, Question
from app.validation.semantic import SemanticValidator, ValidationResult
from app.vault.pipeline import VaultPipeline, VaultTurn

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
# inbox put on a single piece of text; without it a stuck loop ends in LLMContextOverflow. What
# the cap drops is the oldest end of that concatenation (see _text), never the newest answer.
MAX_PROMPT = 4000
# /undo and /cancel arrive without the sender's id (see Orchestrator.undo/cancel), and so does
# the expired-session sweeper; the events row still needs a non-null user column.
NO_USER = 0
# What the audit log records as the "model" of a step the planner had already decided: no model
# read it, so naming one would be a lie.
PLAN_STEP_MODEL = "plan-step"
# How much of the interpreter's reading is passed on to the planner (see _hint).
MAX_HINT = 600
# How alike two names must be to be the same place (see _wrong_place). A page's name spelled
# two ways scores above 0.9; two unrelated pages score around 0.1.
SAME_PLACE = 0.6


def now_utc() -> datetime:
    return datetime.now(UTC)


def _kind(kind: str) -> str:
    return json.dumps({"kind": kind})


def _hint(interp: Interpretation | None) -> str:
    """What the interpreter already worked out about this message, for the planner: its reading
    of the request, the target it settled on, and anything it wanted looked up on the web. A
    hint, not a decision — a single target is often wrong for a goal made of many items."""
    if interp is None:
        return ""
    best = interp.candidates[0] if interp.candidates else None
    parts = [p for p in (
        interp.notes.strip(),
        f"target: {best.target_name}" if best and best.target_name else "",
        f"web: {best.web_query}" if best and best.web_query else "",
    ) if p]
    return "; ".join(parts)[:MAX_HINT]


def _wrong_place(step: StepSpec, interp: Interpretation) -> bool:
    """Did the model answer a plan step with a place nothing like the one the step named?

    Only a plan step is checked, and only when it named a target: the user's own messages are
    read by the model precisely because they do not name things exactly. A loose spelling of
    the right place still passes (a planner's "proekty" for a page called "proyekty"); a
    different page entirely does not."""
    named = step.target.strip()
    best = interp.candidates[0] if interp.candidates else None
    if not named or best is None or not (best.target_name or "").strip():
        return False
    answered = best.target_name.split("[")[0]  # "Books [database]" -> "Books"
    return SequenceMatcher(None, _fold(named), _fold(answered)).ratio() < SAME_PLACE


def _fold(name: str) -> str:
    return " ".join(name.split()).casefold()


def _asked_to_search(raw_text: str, words: tuple[str, ...] = WEB_WORDS) -> bool:
    """Did the user actually ask for something to be looked up? A search is minutes of waiting
    and a page rather than a line, so it takes a word of theirs to start one. The words are
    the user's own vocabulary, so they are editable on the admin page."""
    text = raw_text.lower()
    return any(w in text for w in words)


def _has_something_to_write(candidate: Any) -> bool:
    """Is there anything to write without searching first? A title the user gave is enough; a
    message that names only what to look up ("find a borsch recipe") has nothing else."""
    if (candidate.content or "").strip():
        return True
    return any(f.status == "value" and str(f.value or "").strip()
               for f in candidate.fields.values())


def _abandoned(session: PendingSession) -> str:
    """A plan lives inside the question it is waiting on: when that question expires, the goal
    and every step still to come go with it. Saying so beats letting the user discover it."""
    if not session.plan:
        return ""
    try:
        state = PlanState.model_validate(session.plan)
    except ValidationError:
        return ""
    left = max(len(state.steps) - state.index, 0)
    return texts.PLAN_ABANDONED.format(goal=state.goal, left=left) if left else ""


def _models(calls: list[dict]) -> str:
    """The models a message used, for the row's one model column: "claude-haiku-4-5,
    claude-sonnet-5, plan-step x3"."""
    counts: dict[str, int] = {}
    for c in calls:
        counts[c["model"]] = counts.get(c["model"], 0) + 1
    return ", ".join(m if n == 1 else f"{m} x{n}" for m, n in counts.items())


def _short(text: str, limit: int = 48) -> str:
    """What a log line shows of a message: enough to recognise which one it was."""
    one_line = " ".join(text.split())
    return f'"{one_line[:limit]}…"' if len(one_line) > limit else f'"{one_line}"'


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
    # A multi-step plan this turn is working through (or resumed), and how its current step
    # ended: "executed", "asked" (a question is on screen, the plan waits) or "failed".
    plan: PlanState | None = None
    # The field this message is answering a plan's question about ("author"): once the answer
    # is written, later steps of the same plan reuse it instead of asking again.
    asked_field: str | None = None
    outcome: str = "failed"
    last_undo: str | None = None
    # Sends an intermediate message (a finished plan step) before the turn's own reply.
    progress: Callable[[Reply], Awaitable[None]] | None = None
    # Every model call this message made, in order (see `call`).
    calls: list[dict] = field(default_factory=list)
    # The Obsidian side of this message, running next to everything above, and the execution
    # row this turn wrote (the two meet in _finish_vault).
    vault: asyncio.Task | None = None
    execution_id: int | None = None

    def audit(self, **cols: Any) -> None:
        self.cols.update(cols)

    def call(self, model: str, kind: str, **detail: Any) -> None:
        """One model call of this message. A plan makes several — the interpreter that read the
        message, the planner, a step the planner could not express, the check at the end — and
        the row used to keep the first one's response under the last one's model name, which
        made a plan impossible to follow afterwards. They are all recorded now, in order."""
        self.calls.append({"model": model, "kind": kind,
                           **{k: v for k, v in detail.items() if v is not None}})

    def audit_fallback(self, code: str) -> None:
        self.fallback_error = code


class Orchestrator:
    def __init__(
        self, settings: Settings, discovery: Discovery, builder: ContextBuilder, llm: LLMClient,
        validator: SemanticValidator, policy: Policy, executor: Executor, store: AuditStore,
        sessions: SessionStore, clock: Callable[[], datetime] = now_utc,
        researcher: WebResearcher | None = None, planner: Planner | None = None,
        note: Callable[[], str] | None = None, vault: VaultPipeline | None = None,
        switches: Switches | None = None, tuning: Tuning | None = None,
        health: Health | None = None,
    ) -> None:
        self._s = settings
        self._health = health or Health()
        self._researcher = researcher
        self._planner = planner
        self._vault = vault
        self._switches = switches
        self._tuning = tuning
        # The bot's own name, if it has one: recognised deterministically (app/address.py).
        # Read per message, so renaming it on the admin page needs no restart.
        self._fallback_names = address.names(settings.bot_name)
        self._note = note or (lambda: "")
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
        transcript: str | None = None, progress: Callable[[Reply], Awaitable[None]] | None = None,
    ) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, kind, lambda t: self._text(t, text),
                                    progress=progress, raw_input=text, transcription=transcript)

    async def handle_callback(
        self, chat_id: int, user_id: int, data: str, *,
        progress: Callable[[Reply], Awaitable[None]] | None = None,
    ) -> Reply:
        async with self._chat_lock(chat_id):
            return await self._turn(chat_id, user_id, "callback",
                                    lambda t: self._callback(t, data), progress=progress,
                                    raw_input=data)

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

    @property
    def _names(self) -> tuple[str, ...]:
        return (address.names(self._tuning.bot_name) if self._tuning is not None
                else self._fallback_names)

    @property
    def _web_words(self) -> tuple[str, ...]:
        return self._tuning.web_words if self._tuning is not None else WEB_WORDS

    def _on(self, name: str) -> bool:
        return self._switches.get(name) if self._switches is not None else True

    def _vault_on(self) -> bool:
        return self._vault is not None and self._on("obsidian")

    async def _text(self, turn: _Turn, text: str) -> Reply:
        called = address.strip(text, self._names)
        if called.only_name or (called.called and address.about_question(called.text)):
            # Being called by name with nothing else, or asked what it is: answered from
            # texts.py, with no model call and nothing written anywhere.
            turn.audit(decision=_kind("ABOUT"))
            return Reply(texts.ABOUT.format(name=self._names[0],
                                            digest_at=self._s.daily_digest_at)
                         if not called.only_name else texts.CALLED)
        text = called.text or text
        if not self._on("notion"):
            # Notion is switched off on the admin page: the vault answers on its own, which is
            # what this pipeline was built to be able to do.
            return await self._vault_only(turn, text)
        try:
            snapshot = await self._discovery.get()
        except Exception as e:  # any transport failure reads the same to the user
            log.warning("discovery failed: %s", e)
            return self._plain(turn, "DISCOVERY_FAILED")

        # Ask about expiry before `get`, which drops an expired row without telling anyone.
        expired = self._sessions.pop_expired_one(turn.chat_id, turn.now)
        prefix = await self._expired_prefix(turn, expired) if expired is not None else ""
        session = None if expired is not None else self._sessions.get(turn.chat_id, turn.now)
        if self._vault_on() and session is None:
            # A fresh thought goes to both stores at once. An answer to a question does not:
            # it answers Notion, and the vault has already had the message it belongs to.
            turn.vault = asyncio.create_task(self._vault.handle(text))

        # A live session makes this message a free-text answer: the model sees the question it is
        # answering and both halves of the request, and its fresh interpretation replaces the
        # session outright (an unrelated message is simply a new request, F5).
        pending = self._pending_block(session, snapshot) if session is not None else None
        # A question from a plan's step: this answer finishes the step, then the plan goes on.
        if session is not None and session.plan is not None:
            turn.plan = PlanState.model_validate(session.plan)
            turn.plan.answers.append(text[:MAX_PROMPT])
            turn.asked_field = session.question.field_name
            pending = {**(pending or {}), **plan_context(turn.plan)}
        # Trimmed from the *head*: the newest answer is the part that has to survive. Cutting
        # the tail instead freezes the conversation once the concatenation reaches the cap —
        # every further answer chopped off, the same bytes sent again, the same question asked
        # forever, at one LLM call a turn and with nothing ever reaching Notion or the inbox.
        prompt = (f"{session.original_text}\n{text}"[-MAX_PROMPT:] if session is not None
                  else text)
        asked = list(session.asked) if session is not None else []

        ctx = self._builder.build(snapshot, turn.now, pending, allow_plan=turn.plan is None)
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
        if interp.intent.value == "plan" and turn.plan is None:
            # Even with a clarifying question attached: a plan's side question ("which dates?")
            # is not worth stopping for, and each step can still ask what it really needs.
            return _prefixed(await self._start_plan(turn, prompt, snapshot, interp),
                             prefix)
        result = self._validator.validate(interp, ctx, snapshot)
        decision = self._policy.evaluate(result)
        self._audit_result(turn, result, decision)
        reply = await self._dispatch(turn, decision, result, ctx, prompt, asked)
        if turn.plan is not None:
            reply = await self._after_step(turn, reply)
        return _prefixed(reply, prefix)

    async def _vault_only(self, turn: _Turn, text: str) -> Reply:
        """A message when Notion is off. There is nothing to ask about — the vault never asks —
        so the reply is whatever _finish_vault appends to it."""
        if not self._vault_on():
            return self._plain(turn, "NOTHING_ENABLED")
        turn.audit(decision=_kind("VAULT"))
        turn.source_text = text
        turn.vault = asyncio.create_task(self._vault.handle(text))
        return Reply("")

    async def _dispatch(
        self, turn: _Turn, decision: Decision, result: ValidationResult, ctx: Context, text: str,
        asked: list[str],
    ) -> Reply:
        """Step 7 of the pipeline, shared by a fresh interpretation and a resumed session."""
        if decision.kind == "EXECUTE":
            decision, failed = await self._research(turn, decision, result, text)
            if failed is not None:
                return failed
        if decision.kind == "EXECUTE":
            self._sessions.drop(turn.chat_id)
            return await self._execute(turn, decision, result, text)
        # CLARIFY, plus the two REJECTs that carry a candidate and a question: item_not_found is
        # actionable through its BTN_ADD_NEW button, nothing_to_write through free text. The
        # model's own question needs no candidate — it may have failed to settle on one at all,
        # which is half of why it is asking (see Policy.evaluate).
        if decision.questions and (decision.candidate is not None
                                   or decision.questions[0].type == "clarify"):
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
        except Refused as e:
            # The command was understood and refused for a reason worth saying: an empty page,
            # no rewriter, a model that gave nothing back. Nothing was written, and filing the
            # message in the inbox would only add litter — the user is told and that is all.
            log.info("refused: %s", e.code)
            return self._plain(turn, e.code, **e.fmt)
        except NotionError as e:
            log.warning("execute failed: %s", e)
            code = "NOTION_4XX" if 400 <= e.status < 500 else "NOTION_5XX"
            # The write did not happen, so the text would be lost otherwise.
            return await self._inbox_or_error(turn, text, code, message=e.message)
        turn.audit(executed=1, notion_page_id=executed.page_id)
        self._remember_answer(turn, executed)
        # A search that found nothing did nothing: inside a plan that is a failed step, which
        # the planner should work around rather than count as progress.
        turn.outcome = ("failed" if isinstance(command, Search) and not executed.hits
                        else "executed")
        if executed.undo is not None:
            turn.last_undo = executed.undo.model_dump_json()
        if turn.plan is not None:
            self._note_new_item(command, executed)  # the next step may refer to it by name
        if isinstance(command, Search):
            return Reply(format_search(executed))
        execution_id = self._record_execution(turn, executed)
        return Reply(format_execution(executed, target_url=candidate.target.url),
                     _undo_buttons(execution_id), undo_id=execution_id)

    async def _research(
        self, turn: _Turn, decision: Decision, result: ValidationResult, text: str
    ) -> tuple[Decision, Reply | None]:
        """A web query is looked up before anything is written: what the search finds becomes
        the candidate's content. Returns the decision to carry on with — unchanged, EXECUTE with
        the content filled in, or CLARIFY when the research needs the user first — or a reply
        that ends the turn (no researcher, or nothing found)."""
        candidate = decision.candidate
        if (candidate is None or not candidate.web_query
                or result.intent not in ("create", "append")):
            return decision, None
        if not _asked_to_search(text, self._web_words) and _has_something_to_write(candidate):
            # The model offers a search for "I want to watch the film X" as readily as for
            # "find a borsch recipe". The first costs minutes of waiting, a page instead of a
            # line in the list, and a few cents — for a title the user already gave. Whether a
            # search was asked for is in their own words, so it is decided here, not by a model.
            log.info("no search: nothing in the message asks for one")
            return replace(decision, candidate=replace(candidate, web_query=None)), None
        if self._researcher is None:
            return decision, await self._inbox_or_error(turn, text, "WEB_UNAVAILABLE")
        await self._progress(turn, Reply(texts.SEARCHING_THE_WEB))
        try:
            found = await self._researcher.research(text, candidate.web_query,
                                                    candidate.web_media)
        except ResearchQuestion as e:
            q = Question(type="clarify", target_key=candidate.key, proposed=e.question)
            return replace(decision, kind="CLARIFY", questions=[q], reasons=["clarify"]), None
        except ResearchTimeout as e:
            # Not the same as an empty web: it was cut short, and saying "found nothing" sent
            # the user looking for a search that never finished.
            log.warning("web research timed out: %s", e)
            return decision, await self._inbox_or_error(
                turn, text, "WEB_TIMEOUT", minutes=max(1, round(self._s.research_deadline_s / 60)))
        except ResearchError as e:
            log.warning("web research failed: %s", e)
            return decision, await self._inbox_or_error(turn, text, "WEB_FAILED")
        content = "\n\n".join(part for part in (candidate.content, found) if part)
        return replace(decision, candidate=replace(candidate, content=content)), None

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
        candidate = decision.candidate  # None only for a clarify question (see policy)
        options = options_for(question, candidate, result, ctx)
        session = session_from_decision(
            turn.chat_id, turn.event_id, text, result, replace(decision, questions=[question]),
            options, now=turn.now, ttl_s=self._s.session_ttl_s, asked=asked,
            plan=turn.plan.model_dump() if turn.plan is not None else None,
        )
        turn.outcome = "asked"
        self._sessions.save(session)
        turn.audit(clarification_state=session.model_dump_json())
        return format_question(
            question, [(o.id, o.label) for o in options if o.id not in RESERVED_OPTIONS],
            session.token, inbox=await self._inbox_target(forced=True) is not None,
            target_name=candidate.target.name if candidate else None,
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
        this. Written next to the text so the user can triage the page later — including, when
        the question belonged to a plan, that the rest of the plan never ran. The sweeper has no
        chat to say that in, so the inbox page is the only place it can be said at all."""
        return texts.INBOX_NOTE_UNANSWERED.format(
            question=self._question_text(session, self._target_name(session))) \
            + _abandoned(session)

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

    # ---- multi-step plans ----------------------------------------------------------------------

    async def _start_plan(self, turn: _Turn, text: str, snapshot: WorkspaceSnapshot,
                          interp: Interpretation | None = None) -> Reply:
        """A goal that takes several actions: the planner splits it into one-action steps, which
        then run one by one (see _after_step). The interpreter has already read this message to
        decide it was a plan at all, so its reading goes along as a hint — otherwise that work
        (which target it is about, what to look up on the web) is simply thrown away and the
        planner derives it again from the raw text."""
        turn.audit(decision=_kind("PLAN"))
        if self._planner is None:
            return self._plain(turn, "PLAN_UNAVAILABLE")
        try:
            goal, steps = await self._planner.plan(text, self._workspace(snapshot),
                                                   hint=_hint(interp))
        except PlanError as e:
            log.warning("planning failed: %s", e)
            return await self._inbox_or_error(turn, text, "PLAN_FAILED")
        turn.plan = PlanState(goal=goal, steps=steps)
        turn.call(self._planner.model, "plan", steps=len(steps), goal=goal)
        log.info("llm %s planned %d steps: %s", self._planner.model, len(steps), _short(goal))
        lines = [texts.PLAN_HEADER.format(goal=goal)]
        lines += [f"{i}. {step.text}" for i, step in enumerate(steps, start=1)]
        await self._progress(turn, Reply("\n".join(lines)))
        return await self._after_step(turn, await self._step(turn, steps[0]))

    async def _step(self, turn: _Turn, step: StepSpec) -> Reply:
        """One step of the plan: execute it, or ask the user. A structured step needs no model
        call at all — the planner already decided what it means, and `steps.to_interpretation`
        resolves the names against this snapshot. turn.outcome says which way it went."""
        assert turn.plan is not None
        turn.outcome, turn.last_undo = "failed", None
        try:
            snapshot = await self._discovery.get()
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return Reply(_error("DISCOVERY_FAILED"))
        ctx = self._builder.build(snapshot, turn.now, plan_context(turn.plan), allow_plan=False)
        log.info("step %d/%d: %s", turn.plan.index + 1, len(turn.plan.steps), _short(step.text))
        interp = to_interpretation(step, ctx, turn.plan.field_answers)
        if interp is not None:
            turn.call(PLAN_STEP_MODEL, "step", step=step.text)
            turn.audit(interpretation=interp.model_dump_json())
        else:
            try:
                interp, trace = await self._llm.interpret(step.text, ctx, build_schema(ctx))
            except (LLMUnavailable, LLMInvalidOutput, LLMContextOverflow) as e:
                log.warning("plan step: llm failed: %s", e)
                return Reply(_error("LLM_UNAVAILABLE" if isinstance(e, LLMUnavailable)
                                    else "LLM_INVALID_OUTPUT"))
            self._audit_llm(turn, interp, trace)
            if _wrong_place(step, interp):
                # The step named a place, the model answered with a different one — which is
                # what it does when the named place is missing from the context: it picks the
                # nearest key and says so confidently. A step of a plan writes without asking
                # anyone, so a guess like that lands a page of text in an unrelated page.
                log.warning("plan step names %r, model answered %r: failing the step",
                            step.target, interp.candidates[0].target_name)
                return Reply(_error("STEP_WRONG_TARGET", target_name=step.target))
        result = self._validator.validate(interp, ctx, snapshot)
        decision = self._policy.evaluate(result)
        self._audit_result(turn, result, decision)
        return await self._dispatch(turn, decision, result, ctx, step.text, [])

    async def _after_step(self, turn: _Turn, reply: Reply) -> Reply:
        """Record the step that just ended and keep going. The planner is asked what to do next
        only when it can change anything: after a step that failed, or once the planned steps
        are done — a plan whose steps all work costs no check calls at all. A step that asks the
        user something stops here: its question is the reply, and its session carries the plan,
        so the answer picks the loop up again."""
        state = turn.plan
        assert state is not None
        while True:
            if turn.outcome == "asked":
                return reply
            failed = turn.outcome != "executed"
            current = state.current
            state.history.append(PlanStep(
                request=current.text if current is not None else state.goal,
                outcome=reply.text[:500], status="failed" if failed else "done"))
            if turn.last_undo is not None:
                state.undo.append(turn.last_undo)
            await self._progress(turn, replace(
                reply, text=texts.PLAN_STEP.format(n=len(state.history), text=reply.text)))
            if state.exhausted:
                return self._plan_done(turn, "", stopped=True)
            state.index += 1
            if state.current is None and not failed and not state.failures:
                # Every planned step did what it said. There is nothing left for the planner to
                # decide, and asking it anyway costs a Sonnet call on the end of every plan —
                # two of them for the two lines this started as.
                return self._plan_done(turn, state.goal)
            if failed or state.current is None:
                verdict = await self._check(turn, state)
                if verdict is None or verdict.done:
                    return self._plan_done(turn, verdict.summary if verdict else "")
                state.add(verdict.next_step)
            reply = await self._step(turn, state.current)

    async def _check(self, turn: _Turn, state: PlanState) -> Verdict | None:
        """Ask the planner what to do next; None when the plan should simply stop."""
        assert self._planner is not None
        try:
            snapshot = await self._discovery.get()
        except Exception as e:
            log.warning("discovery failed: %s", e)
            return None
        verdict = await self._planner.next(state, self._workspace(snapshot))
        turn.call(self._planner.model, "check", done=verdict.done, next_step=verdict.next_step)
        log.info("llm %s checked the plan: %s", self._planner.model,
                 "done" if verdict.done else _short(verdict.next_step))
        return verdict

    def _plan_done(self, turn: _Turn, summary: str, *, stopped: bool = False) -> Reply:
        """The plan's closing message, with one button that undoes every write it made."""
        state = turn.plan
        assert state is not None
        turn.plan = None
        done = sum(1 for s in state.history if s.status == "done")
        template = texts.PLAN_STOPPED if stopped else texts.PLAN_DONE
        text = template.format(done=done, total=len(state.history), summary=summary or state.goal)
        if not state.undo:
            return Reply(text)
        batch = UndoRecord(kind="batch",
                           batch=[UndoRecord.model_validate_json(u) for u in state.undo])
        execution_id = turn.execution_id = self._store.add_execution(
            turn.event_id, turn.chat_id, None, batch.model_dump_json(),
            self._clock() + timedelta(seconds=self._s.undo_window_s),  # from the last write
        )
        return Reply(text, [[Button(f"u:{execution_id}", texts.BTN_UNDO_ALL)]],
                     undo_id=execution_id)

    def _workspace(self, snapshot: WorkspaceSnapshot) -> str:
        return workspace_summary([t for t in snapshot.targets if not t.hidden], self._note())

    @staticmethod
    async def _progress(turn: _Turn, reply: Reply) -> None:
        if turn.progress is None:
            return
        try:
            await turn.progress(reply)
        except Exception:  # a lost progress line must not stop the plan
            log.exception("could not send a plan progress message")

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
            return await self._cancelled()
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
            # The session stays: the next message answers it. A correction to where or what
            # (target/intent) is re-read together with the original message, so say so.
            if session.question.type in ("target", "intent_confirm"):
                return Reply(texts.ENTER_CORRECTION)
            return Reply(texts.ENTER_VALUE)
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
        if session.plan is not None:
            turn.plan = PlanState.model_validate(session.plan)
            # The answered question named a field; once this step writes it, the plan's later
            # steps take the same value rather than asking again (_remember_answer).
            turn.asked_field = session.question.field_name
        reply = await self._dispatch(turn, decision, result, ctx, session.original_text,
                                     list(session.asked))
        if turn.plan is not None:
            reply = await self._after_step(turn, reply)
        return reply

    async def _undo(self, turn: _Turn, execution_id: int | None) -> Reply:
        minutes = max(1, self._s.undo_window_s // 60)
        row = (self._store.get_execution(execution_id, turn.now) if execution_id is not None
               else self._store.latest_execution(turn.chat_id, turn.now))
        if row is None or row["undone"] or row["chat_id"] != turn.chat_id:
            return self._plain(turn, "UNDO_EXPIRED", minutes=minutes)
        record = UndoRecord.model_validate_json(row["undo"])
        try:
            await self._executor.undo(record)
        except NotionError as e:
            log.warning("undo failed: %s", e)
            return self._plain(turn, "UNDO_FAILED", message=e.message)
        if record.vault and self._vault is not None:
            try:
                await self._vault.undo(record.vault)
            except Exception:  # Notion is already back: say so rather than fail the undo
                log.exception("undoing the vault side failed")
        self._store.mark_undone(row["id"])
        turn.audit(decision=_kind("UNDO"))
        return Reply(texts.UNDONE)

    async def _cancel(self, turn: _Turn) -> Reply:
        self._sessions.drop(turn.chat_id)
        turn.audit(decision=_kind("CANCEL"))
        return await self._cancelled()

    async def _cancelled(self) -> Reply:
        """Cancel throws the message away. While no inbox page is flagged there was no inbox
        button to keep it instead, and nothing in the chat says why — so the reply
        says where to set one up. Once one is flagged, or the inbox is switched off on purpose
        (INBOX_MODE=off), it is just the one line."""
        if self._s.inbox_mode == "off" or await self._inbox_target(forced=True) is not None:
            return Reply(texts.CANCELLED)
        port = self._s.admin_ui_port
        hint = (texts.CANCELLED_NO_INBOX.format(admin_url=f"http://127.0.0.1:{port}")
                if port > 0 else texts.CANCELLED_NO_INBOX_NO_ADMIN)
        return Reply(f"{texts.CANCELLED}\n{hint}")

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
        if turn.plan is not None:
            # A failed step is the planner's to handle (retry differently, skip); filing each
            # step's command in the inbox would only litter it.
            turn.outcome = "failed"
            return Reply(_error(code, **fmt))
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
        return template.format(target_name=target.name) + _abandoned(expired)

    # ---- audit -------------------------------------------------------------------------------

    async def _turn(
        self, chat_id: int, user_id: int, kind: str,
        work: Callable[[_Turn], Awaitable[Reply]], *,
        progress: Callable[[Reply], Awaitable[None]] | None = None, **cols: Any,
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
        turn.progress = progress
        # Every log record produced while this turn is in flight — here, and in every module the
        # turn calls into (discovery, the LLM client, the executor) — carries this event's id, so
        # a log line can be traced back to its `events` row. The id cannot be bound any earlier:
        # it does not exist until `_open` above has returned, and no caller upstream of this
        # method (a Telegram handler) ever sees it at all.
        with (bind_event(turn.event_id), notion_stats.collect() as calls,
              titles.collect()):
            try:
                reply = await work(turn)
            except Exception:  # the caller is a chat handler: it gets a Reply, always
                log.exception("unhandled failure in event %s", turn.event_id)
                reply = self._plain(turn, "INTERNAL")
            try:
                reply = await self._finish_vault(turn, reply)
            except Exception:  # the Notion answer is already earned
                log.exception("obsidian side failed in event %s", turn.event_id)
            # Last line of every reply, at most once an hour: why the answers got worse. It
            # goes here rather than in any one branch because every branch above can be the
            # degraded one — a question the local model asked, a vault line that says nothing
            # was written, a plan that stopped.
            warning = self._health.note()
            if warning:
                reply = replace(reply, text=f"{reply.text}\n\n{warning}".strip())
            try:
                self._finish(turn)
            except Exception:  # the answer is already earned; losing the row must not eat it
                log.exception("could not close audit event %s", turn.event_id)
            self._log_turn(turn, calls)
        return reply

    def _note_new_item(self, command: Command, executed: ExecutionResult) -> None:
        """What this step added, handed to discovery so the next step can name it without the
        whole workspace being re-read. A write that added nothing nameable (an append, an
        update) leaves the snapshot as it is."""
        if isinstance(command, CreateItem):
            title = next((p.value for p in command.properties if p.type == "title"), "")
            target_id: str = command.data_source_id
        elif isinstance(command, CreatePage):
            if executed.page_id:
                # A page is a place of its own, not only a child of the one it sits in: the
                # next step of the plan will want to write into it by name.
                self._discovery.note_new_page(executed.page_id, command.title,
                                              command.parent_page_id, executed.url or "")
            title, target_id = command.title, command.parent_page_id
        else:
            return
        if executed.page_id and isinstance(title, str):
            self._discovery.note_new_item(target_id, executed.page_id, title,
                                          executed.url or "")

    @staticmethod
    def _remember_answer(turn: _Turn, executed: ExecutionResult) -> None:
        """The user answered a plan's question about a field, and this step has now written a
        value for it: the rest of the plan takes the same value rather than asking per step.
        Only the field that was actually asked about — never what the planner filled in
        itself, which is a guess the user never saw."""
        if turn.plan is None or not turn.asked_field:
            return
        for written in executed.written:
            if written.name != turn.asked_field:
                continue
            value = written.value
            text = value.get("name") if isinstance(value, dict) else value
            if isinstance(text, str) and text.strip():
                turn.plan.field_answers[written.name] = text
            return

    @staticmethod
    def _log_turn(turn: _Turn, calls: notion_stats.Tally) -> None:
        """One line per message: what it was decided to be, and what Notion it took. The
        per-call lines are the LLM ones; this is the receipt at the end."""
        decision = json.loads(turn.cols.get("decision") or "{}").get("kind", "-")
        notion = notion_stats.summary(calls)
        log.info("done: %s in %.1fs%s", decision.lower(),
                 (monotonic() - turn.started), f" | notion {notion}" if notion else "")

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
        if turn.calls:
            turn.audit(llm_model=_models(turn.calls),
                       llm_response=json.dumps(turn.calls, ensure_ascii=False))
        self._store.update_event(
            turn.event_id, duration_ms=int((monotonic() - turn.started) * 1000), **turn.cols)

    def _plain(self, turn: _Turn, code: str, **fmt: Any) -> Reply:
        turn.audit(error=code, decision=json.dumps({"kind": "ERROR", "code": code}))
        return Reply(_error(code, **fmt))

    async def _finish_vault(self, turn: _Turn, reply: Reply) -> Reply:
        """Wait for the Obsidian side of this message, say in one line what it did, and put its
        undo where the reply's own Undo button will find it.

        The two sides are joined here and nowhere else: whatever the Notion side ended as — a
        write, a question, a rejection — the vault has already written, and the user is told so.
        When Notion wrote too, both undos live in that write's record, so one button reverts
        both; when it did not, the vault's undo gets a row of its own, which /undo still finds."""
        if turn.vault is None:
            return reply
        try:
            result: VaultTurn = await turn.vault
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("the obsidian side of event %s failed", turn.event_id)
            return reply
        if result.model and (result.writes or result.error):
            turn.call(result.model, "vault", writes=len(result.writes) or None,
                      error=result.error or None,
                      prompt_tokens=result.prompt_tokens, output_tokens=result.output_tokens)
        undos = result.undos
        if undos:
            self._record_vault_undo(turn, undos)
        line = result.reply_line()
        return replace(reply, text=f"{reply.text}\n{line}".strip()) if line else reply

    def _record_vault_undo(self, turn: _Turn, undos: list[Any]) -> None:
        if turn.execution_id is not None:
            row = self._store.get_execution(turn.execution_id, turn.now)
            if row is not None:
                record = UndoRecord.model_validate_json(row["undo"])
                record.vault = undos
                self._store.update_execution_undo(turn.execution_id, record.model_dump_json())
                return
        # Notion wrote nothing this turn (it asked a question, or could not place the message):
        # the vault's own row carries no Notion part, and /undo is what reaches it.
        turn.execution_id = self._store.add_execution(
            turn.event_id, turn.chat_id, None,
            UndoRecord(kind="vault", vault=undos).model_dump_json(),
            self._clock() + timedelta(seconds=self._s.undo_window_s),
        )

    def _record_execution(self, turn: _Turn, result: ExecutionResult) -> int | None:
        """Executions are rows only when they can be reverted; `reply_message_id` is filled in by
        Plan 3b after the reply is actually sent (there is no message id at this point)."""
        if result.undo is None:
            return None
        # Measured from the write, not from when the message arrived: a web search can spend
        # four minutes before anything is written, and undo used to expire during it — the user
        # pressed the button two minutes after the page appeared and was told it was too late.
        turn.execution_id = self._store.add_execution(
            turn.event_id, turn.chat_id, None, result.undo.model_dump_json(),
            self._clock() + timedelta(seconds=self._s.undo_window_s),
        )
        return turn.execution_id

    @staticmethod
    def _audit_llm(turn: _Turn, interp: Interpretation, trace: LLMTrace) -> None:
        best = interp.candidates[0] if interp.candidates else None
        # What the message said stays out of the log above DEBUG (tests/test_security.py); the
        # event id in every line is the key back to the audit row, which has the text itself.
        log.info(
            "llm %s read it as %s%s | %.1fs%s%s",
            trace.model, interp.intent.value,
            f" -> {best.target_name}" if best and best.target_name else "",
            trace.duration_ms / 1000,
            f" | {trace.prompt_tokens}+{trace.output_tokens} tok"
            if trace.prompt_tokens else "",
            f" | {trace.attempts} attempts" if trace.attempts > 1 else "",
        )
        log.debug("llm %s: %s", trace.model, _short(turn.source_text or "", 200))
        # The events table has no token columns, so the counts ride along with the response
        # they describe. trace.messages (the prompt) is already in llm_context.
        turn.call(trace.model, "interpret", raw=trace.raw_response, attempts=trace.attempts,
                  duration_ms=trace.duration_ms, prompt_tokens=trace.prompt_tokens,
                  output_tokens=trace.output_tokens, done_reason=trace.done_reason)
        turn.audit(interpretation=interp.model_dump_json())

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
