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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from app import texts
from app.vault import agenda as agenda_mod
from app.vault.filer import Filer, FilerError, check, context
from app.vault.index import VaultIndex
from app.vault.linker import Linker
from app.vault.search import Hit, search, vault_name
from app.vault.writer import VaultAction, VaultUndo, VaultWrite, VaultWriter

log = logging.getLogger(__name__)

MAX_SUMMARY = 3


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
                 linking: Callable[[], bool] = lambda: True) -> None:
        self._index = index
        self._writer = writer
        self._filer = filer
        self._linker = linker
        self._now = now
        self._linking = linking
        self._tasks: set[asyncio.Task] = set()  # linking, running behind the reply

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self._filer.aclose()
        if self._linker is not None:
            await self._linker.aclose()

    async def handle(self, message: str) -> VaultTurn:
        """Read the message, write the vault, and start the linking behind the reply."""
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
        actions = check(raw, self._index, message)
        if not actions:  # the model answered nothing usable: keep the words rather than drop them
            actions = [VaultAction(action="inbox", text=message)]
        turn = VaultTurn(model=self._filer.model, prompt_tokens=prompt_tokens,
                         output_tokens=output_tokens, vault=vault_name(self._index.root))
        dated = [a for a in actions if a.action == "agenda"]
        questions = [a for a in actions if a.action == "search"]
        actions = [a for a in actions if a.action not in ("search", "agenda")]
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
                            *([f"search:{len(turn.hits)} hits"] if turn.asked else [])]) or "-")
        self._link_later(turn.writes)
        return turn

    def _agenda_answer(self, action: VaultAction) -> str:
        """A question about dates, answered from the vault: a day, a range, or "what now"."""
        today = self._now().date()
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
        return agenda_mod.digest(agenda_mod.build(self._index, day), day)

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
