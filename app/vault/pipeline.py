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
from dataclasses import dataclass, field
from datetime import datetime

from app import texts
from app.vault.filer import Filer, FilerError, check, context
from app.vault.index import VaultIndex
from app.vault.linker import Linker
from app.vault.writer import VaultAction, VaultUndo, VaultWrite, VaultWriter

log = logging.getLogger(__name__)

MAX_SUMMARY = 3


@dataclass
class VaultTurn:
    writes: list[VaultWrite] = field(default_factory=list)
    error: str = ""
    model: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def undos(self) -> list[VaultUndo]:
        return [w.undo for w in self.writes if w.undo is not None]

    def reply_line(self) -> str:
        if self.error:
            return texts.VAULT_FAILED.format(error=self.error)
        if not self.writes:
            return ""
        what = ", ".join(w.what for w in self.writes[:MAX_SUMMARY])
        if len(self.writes) > MAX_SUMMARY:
            what += f" (+{len(self.writes) - MAX_SUMMARY})"
        return texts.VAULT_REPLY.format(what=what)


class VaultPipeline:
    def __init__(self, index: VaultIndex, writer: VaultWriter, filer: Filer,
                 linker: Linker | None = None, *, now=datetime.now) -> None:
        self._index = index
        self._writer = writer
        self._filer = filer
        self._linker = linker
        self._now = now
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
            return VaultTurn(error=str(e), model=self._filer.model)
        except OSError as e:
            log.warning("vault unreadable: %s", e)
            return VaultTurn(error=type(e).__name__)
        actions = check(raw, self._index, message)
        if not actions:  # the model answered nothing usable: keep the words rather than drop them
            actions = [VaultAction(action="inbox", text=message)]
        turn = VaultTurn(model=self._filer.model, prompt_tokens=prompt_tokens,
                         output_tokens=output_tokens)
        try:
            turn.writes = await asyncio.to_thread(self._write_all, actions)
        except (OSError, ValueError) as e:
            log.warning("vault write failed: %s", e)
            turn.error = type(e).__name__
            return turn
        log.info("vault %s wrote %s", self._filer.model,
                 ", ".join(f"{w.kind}:{w.note}" for w in turn.writes))
        self._link_later(turn.writes)
        return turn

    def _write_all(self, actions: list[VaultAction]) -> list[VaultWrite]:
        return [self._writer.run(a) for a in actions]

    def _link_later(self, writes: list[VaultWrite]) -> None:
        if self._linker is None or not writes:
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
