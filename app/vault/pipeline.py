"""The Obsidian side of a message, end to end and on its own.

It shares nothing with the Notion pipeline but the message itself: its own interpreter (the
filer), its own check, its own writer, its own undo. Notion can be switched off without
touching any of it.

It never asks the user anything and never fails a message: a model that is down, an action that
does not check out, a note that is gone — all of it ends as a line in the inbox note or as one
line in the reply saying nothing was written."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from app import texts
from app.llm.edits import EditError, Editor
from app.llm.rewrite import RewriteError, Rewriter
from app.vault import agenda as agenda_mod
from app.vault import frontmatter, mdedit
from app.vault.filer import GROCERY_LIST, Filer, FilerError, check, context
from app.vault.index import VaultIndex
from app.vault.linker import Linker
from app.vault.search import Hit, search, vault_name
from app.vault.writer import VaultAction, VaultUndo, VaultWrite, VaultWriter

log = logging.getLogger(__name__)

MAX_SUMMARY = 3
# Where a plan step's text goes when the filer picked no note for it. A folder, not the
# inbox note: the inbox keeps one-line reminders, and this is a page of research.
CARRIERS = ("note", "append", "update", "rewrite")
MAX_TITLE = 60


def with_content(actions: list[VaultAction], content: str,
                 message: str) -> list[VaultAction]:
    """Text a plan step has already produced, put into whichever action files it.

    The filer chose the place; the words are not its to rewrite, and it never saw them —
    what a web search brought back is data, and data does not go into a prompt. When the
    filer picked nothing that can hold a body, the text becomes a note of its own rather
    than a truncated line in the inbox."""
    lines = content.splitlines()
    out, used = [], False
    for action in actions:
        if not used and action.action in CARRIERS:
            action = action.model_copy(update={"body": lines})
            used = True
        out.append(action)
    if not used:
        title = (message.strip() or texts.VAULT_INBOX_NOTE)[:MAX_TITLE]
        out.append(VaultAction(action="note", folder=texts.VAULT_NOTES_DIR,
                               title=title, body=lines))
    return out


def _day(value: str) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


@dataclass
class VaultTurn:
    writes: list[VaultWrite] = field(default_factory=list)
    # A question answered from the vault: its hits, and the vault's name for the links.
    hits: list[Hit] = field(default_factory=list)
    asked: bool = False
    # An answer built from dates rather than from words: the agenda (see app/vault/agenda.py).
    answer: str = ""
    vault: str = ""
    error: str = ""
    # Why Claude could not be used, when that is what went wrong: a code from
    # app/llm/health.py, so the reply names the cause in the user's own language rather than
    # showing them "claude 400".
    reason: str = ""
    model: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def undos(self) -> list[VaultUndo]:
        return [w.undo for w in self.writes if w.undo is not None]

    def reply_line(self) -> str:
        if self.error:
            why = texts.LLM_DOWN_SHORT.get(self.reason) or self.error
            return texts.VAULT_FAILED.format(error=why)
        if self.answer and not self.writes:
            return self.answer
        if self.asked and not self.writes:
            return self._found()
        if not self.writes:
            return ""
        what = ", ".join(w.what for w in self.writes[:MAX_SUMMARY])
        if len(self.writes) > MAX_SUMMARY:
            what += f" (+{len(self.writes) - MAX_SUMMARY})"
        line = texts.VAULT_REPLY.format(what=what)
        if self.answer:
            return f"{line}\n{self.answer}"
        return f"{line}\n{self._found()}" if self.asked else line

    def _found(self) -> str:
        """The answer to a question, one line per hit, each a link that opens the note in
        Obsidian — on the phone too."""
        if not self.hits:
            return texts.VAULT_SEARCH_EMPTY
        lines = [texts.VAULT_SEARCH_HEADER]
        for hit in self.hits:
            if hit.kind == "task":
                lines.append(texts.VAULT_SEARCH_TASK.format(line=hit.line))
                continue
            name = f"[{hit.name}]({hit.uri(self.vault)})" if self.vault else hit.name
            lines.append(texts.VAULT_SEARCH_HIT.format(
                name=name, line=f" — {hit.line}" if hit.line else ""))
        return "\n".join(lines)


class VaultPipeline:
    def __init__(self, index: VaultIndex, writer: VaultWriter, filer: Filer,
                 linker: Linker | None = None, *, now=datetime.now,
                 linking: Callable[[], bool] = lambda: True,
                 rewriter: Rewriter | None = None,
                 editor: Editor | None = None) -> None:
        self._index = index
        self._writer = writer
        self._filer = filer
        self._linker = linker
        self._rewriter = rewriter
        self._editor = editor
        self._now = now
        self._linking = linking
        self._tasks: set[asyncio.Task] = set()  # linking, running behind the reply

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._filer.aclose()
        if self._linker is not None:
            await self._linker.aclose()
        if self._rewriter is not None:
            await self._rewriter.aclose()
        if self._editor is not None:
            await self._editor.aclose()

    async def handle(self, message: str, *, content: str = "",
                     go: Callable[[], Awaitable[bool]] | None = None) -> VaultTurn:
        """Read the message, write the vault, and start the linking behind the reply.

        `content` is text the caller already has (a plan step's research): the filer still
        decides where it goes, but it is written as it stands. `go` is awaited before
        anything is written, so a caller that started this in parallel and then learned it
        was not wanted can stop it without a half-finished write — cancelling the task
        could not, because the writes happen in a thread."""
        try:
            await asyncio.to_thread(self._index.refresh)
            ctx = context(self._index, message, self._now())
            raw, prompt_tokens, output_tokens = await self._filer.file(message, ctx)
        except FilerError as e:
            log.warning("filer failed: %s", e)
            return VaultTurn(error=str(e), reason=e.reason, model=self._filer.model)
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            return VaultTurn(error=type(e).__name__)
        if go is not None and not await go():
            log.info("vault: the message turned out to be a plan; its steps write instead")
            return VaultTurn(model=self._filer.model, prompt_tokens=prompt_tokens,
                             output_tokens=output_tokens)
        actions = check(raw, self._index, message)
        if not actions:  # the model answered nothing usable: keep the words rather than drop them
            actions = [VaultAction(action="inbox", text=message)]
        if content:
            actions = with_content(actions, content, message)
        turn = VaultTurn(model=self._filer.model, prompt_tokens=prompt_tokens,
                         output_tokens=output_tokens, vault=vault_name(self._index.root))
        dated = [a for a in actions if a.action == "agenda"]
        questions = [a for a in actions if a.action == "search"]
        shopping = [a for a in actions
                    if a.action == "grocery" and a.scope == GROCERY_LIST]
        actions = [a for a in actions if a.action not in ("search", "agenda")
                   and a not in shopping]
        for _ in shopping[:1]:  # one answer however many times it was asked for
            answer = await asyncio.to_thread(self._grocery_answer)
            turn.answer = f"{turn.answer}\n{answer}".strip() if turn.answer else answer
        # A rewrite is the one action the filer cannot finish on its own: it needs the
        # note's current text, which the filer never saw. Each one becomes an action
        # carrying the finished text, or is dropped with a line in the reply.
        prepared = [await self._rewritten(a, turn) if a.action == "rewrite" else a
                    for a in actions]
        actions = [a for a in prepared if a is not None]
        for question in dated:
            answer = await asyncio.to_thread(self._agenda_answer, question)
            turn.answer = f"{turn.answer}\n{answer}".strip() if turn.answer else answer
        for question in questions:
            turn.asked = True
            turn.hits += await asyncio.to_thread(
                search, self._index, question.text, folder=question.folder,
                tags=tuple(question.tags), props=question.props)
        try:
            turn.writes = await asyncio.to_thread(self._write_all, actions)
        except (OSError, ValueError) as e:
            log.warning("vault write failed: %s", e)
            turn.error = type(e).__name__
            return turn
        log.info("vault %s: %s", self._filer.model,
                 ", ".join([*(f"{w.kind}:{w.note}" for w in turn.writes),
                            *([f"search:{len(turn.hits)} hits"] if turn.asked else []),
                            # Without this an answered question looked exactly like a turn
                            # that did nothing at all, which is how the overdue bug hid.
                            *([f"answered:{len(turn.answer.splitlines())} lines"]
                              if turn.answer else [])]) or "-")
        self._link_later(turn.writes)
        return turn

    async def _rewritten(self, action: VaultAction, turn: VaultTurn) -> VaultAction | None:
        """The same action with `body` filled in, or None when nothing could be changed and
        saying so beats writing something.

        One model call decides whether this is a few edits or a whole new text; the note (or the
        named section) is read once, and the edits are applied to exactly the lines that were
        numbered. A heading that is no longer there widens the change to the whole note rather
        than failing: the user asked for the note to change, and the section was only how they
        pointed.

        Undo needs nothing special here. The writer keeps the file's whole previous text, so
        putting a note back is exact — including its pictures, whose files are never deleted."""
        note = self._index.by_name(action.note)
        if note is None:
            return None
        if self._editor is None and self._rewriter is None:
            turn.error = turn.error or texts.VAULT_REWRITE_OFF
            return None
        try:
            text = await asyncio.to_thread(self._index.read, note.path)
        except OSError as e:
            log.warning("could not read %s to change it: %s", note.path, e)
            return None
        # The note without its frontmatter: the writer puts the properties back, so a line
        # number the model is given has to mean the same line the writer will change.
        _, note_body = frontmatter.split(text)
        current, heading = note_body, action.heading
        if heading:
            lines = note_body.split("\n")
            section = mdedit.find_section(lines, heading)
            if section is None:
                heading = ""
            else:
                current = "\n".join(lines[section.start:section.end])
        body = await self._new_body(note.name, current, action.text, turn)
        if body is None:
            return None
        return action.model_copy(update={"heading": heading, "body": body})

    async def _new_body(self, name: str, current: str, instruction: str,
                        turn: VaultTurn) -> list[str] | None:
        """The note's (or section's) new lines: a few edits applied, or a whole new text."""
        lines = current.split("\n")
        if self._editor is not None:
            try:
                plan, prompt_tokens, output_tokens = await self._editor.plan(
                    mdedit.numbered(lines), instruction)
            except EditError as e:
                return self._failed(name, e, turn)
            turn.prompt_tokens += prompt_tokens
            turn.output_tokens += output_tokens
            if plan.edits:
                # A note is the user's own text file and the previous version is kept whole, so
                # the only rule worth enforcing is that an edit may not point outside what the
                # model was shown; `apply_edits` ignores anything that does.
                return mdedit.apply_edits(lines, plan.edits)
            if plan.full.strip():
                return plan.full.splitlines()
            return None
        assert self._rewriter is not None
        try:
            new_text, prompt_tokens, output_tokens = await self._rewriter.rewrite(
                current, instruction)
        except RewriteError as e:
            return self._failed(name, e, turn)
        turn.prompt_tokens += prompt_tokens
        turn.output_tokens += output_tokens
        return new_text.splitlines()

    @staticmethod
    def _failed(name: str, error: Exception, turn: VaultTurn) -> None:
        log.warning("changing %s failed: %s", name, error)
        turn.error = turn.error or str(error)
        turn.reason = turn.reason or getattr(error, "reason", "")
        return None

    def _grocery_answer(self) -> str:
        """What still has to be bought, or that nothing does."""
        from app.vault import groceries

        want = groceries.needed(self._index.groceries())
        if not want:
            return texts.VAULT_GROCERIES_EMPTY
        return texts.VAULT_GROCERIES_LIST.format(items=", ".join(want))

    def _agenda_answer(self, action: VaultAction) -> str:
        """A question about dates, answered from the vault: a day, a range, or "what now"."""
        today = self._now().date()
        if action.scope == "overdue":
            # Everything late, oldest first — the question asks for the list, not for a
            # suggestion of what to start with.
            return agenda_mod.listing(agenda_mod.build(self._index, today).overdue,
                                      texts.VAULT_OVERDUE)
        if action.scope == "now":
            return agenda_mod.listing(agenda_mod.suggest(self._index, today), texts.VAULT_NOW)
        start = _day(action.due_from) or _day(action.due_to) or today
        end = _day(action.due_to) or start
        if end < start:
            start, end = end, start
        items = agenda_mod.on_day(self._index, start, end)
        header = (texts.VAULT_ON_DAY.format(date=start.strftime("%d.%m")) if start == end
                  else texts.VAULT_ON_RANGE.format(start=start.strftime("%d.%m"),
                                                   end=end.strftime("%d.%m")))
        return agenda_mod.listing(items, header)

    def digest(self, today: date | None = None) -> str:
        """The morning message: overdue, today, tomorrow, and the meetings around them. An
        empty string when there is nothing to say — nobody wants "you have 0 tasks"."""
        self._index.refresh()
        day = today or self._now().date()
        text = agenda_mod.digest(agenda_mod.build(self._index, day), day)
        shopping = agenda_mod.groceries_line(self._index)
        if not shopping:
            return text
        return f"{text}\n\n{shopping}" if text else shopping

    def _write_all(self, actions: list[VaultAction]) -> list[VaultWrite]:
        return [self._writer.run(a) for a in actions]

    def _link_later(self, writes: list[VaultWrite]) -> None:
        if self._linker is None or not writes or not self._linking():
            return
        task = asyncio.create_task(self._link(writes))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _link(self, writes: list[VaultWrite]) -> None:
        for write in writes:
            try:
                await self._linker.link(write)
            except Exception as e:  # never reaches the user: the note is already written
                log.warning("linking %s failed: %s", write.note, type(e).__name__)

    async def undo(self, undos: list[VaultUndo]) -> None:
        """Put every file of one turn back, newest first."""
        for undo in reversed(undos):
            await asyncio.to_thread(self._writer.undo, undo)
