"""One digest run: read what arrived since last time, sort it, and write the message.

Read-only by construction — the mailbox is opened read-only and no code path here can change a
message. State (where the last run stopped) lives in a small JSON file next to the database."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app import texts
from app.mail.classify import OTHER, Classifier, Sorted
from app.mail.imap import GmailIMAP, MailboxError, Message

log = logging.getLogger(__name__)

MAX_PER_BUCKET = 8
FIRST_RUN_HOURS = 12


@dataclass
class MailRun:
    sorted: list[Sorted] = field(default_factory=list)
    error: str = ""
    prompt_tokens: int = 0
    output_tokens: int = 0

    @property
    def empty(self) -> bool:
        return not self.sorted and not self.error


class MailState:
    """Where the last run stopped: the newest uid it saw, and the mailbox generation that uid
    belongs to. A changed UIDVALIDITY means Gmail renumbered everything, so the uid is dropped
    rather than trusted."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def read(self) -> tuple[str | None, str]:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            return raw.get("uid") or None, str(raw.get("validity", ""))
        except (OSError, ValueError):
            return None, ""

    def write(self, uid: str | None, validity: str) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_name(self._path.name + ".tmp")
            tmp.write_text(json.dumps({"uid": uid, "validity": validity}), encoding="utf-8")
            tmp.replace(self._path)
        except OSError as e:
            log.warning("could not save the mail state: %s", e)


def digest(run: MailRun, buckets: list[str]) -> str:
    """The message, grouped by bucket in the order the user configured them."""
    if run.error:
        return texts.MAIL_FAILED.format(error=run.error)
    if not run.sorted:
        return ""
    grouped: OrderedDict[str, list[Sorted]] = OrderedDict((b, []) for b in buckets)
    for item in run.sorted:
        grouped.setdefault(item.bucket, []).append(item)
    lines = [texts.MAIL_HEADER.format(n=len(run.sorted))]
    for bucket, items in grouped.items():
        if not items:
            continue
        lines.append(texts.MAIL_BUCKET.format(bucket=bucket, n=len(items)))
        for item in items[:MAX_PER_BUCKET]:
            lines.append(texts.MAIL_ITEM.format(sender=item.message.short_sender,
                                                 summary=item.summary))
        if len(items) > MAX_PER_BUCKET:
            lines.append(texts.MAIL_MORE.format(n=len(items) - MAX_PER_BUCKET))
    return "\n".join(lines)


class MailService:
    def __init__(self, mailbox: GmailIMAP, classifier: Classifier, state: MailState,
                 *, buckets: list[str] | None = None, max_per_run: int = 40,
                 source: Callable[[], tuple[list[str], dict[str, str]]] | None = None) -> None:
        self._box = mailbox
        self._classifier = classifier
        self._state = state
        self.buckets = buckets or classifier.buckets
        self._source = source  # the admin page's text, re-read on every run
        self._max = max_per_run

    async def aclose(self) -> None:
        await self._classifier.aclose()

    def _refresh_buckets(self) -> None:
        """Whatever the user has on the admin page right now. A change needs no restart."""
        if self._source is None:
            return
        names, meanings = self._source()
        if not names:
            return
        self._classifier.buckets = ([*names, OTHER] if OTHER not in names else list(names))
        self._classifier.meanings = meanings
        self.buckets = self._classifier.buckets

    async def run(self) -> MailRun:
        """Fetch, sort, and remember where we stopped. Never raises: a mailbox that is down
        becomes one line in the digest, and the next run picks up from the same place."""
        self._refresh_buckets()
        uid, validity = self._state.read()
        try:
            messages, newest, now_validity = await asyncio.to_thread(
                self._box.fetch_since, uid, FIRST_RUN_HOURS, self._max)
        except MailboxError as e:
            log.warning("mailbox unreachable: %s", e)
            return MailRun(error=str(e))
        if validity and now_validity and validity != now_validity:
            log.info("the mailbox was renumbered; starting from the last %d hours",
                     FIRST_RUN_HOURS)
            try:
                messages, newest, now_validity = await asyncio.to_thread(
                    self._box.fetch_since, None, FIRST_RUN_HOURS, self._max)
            except MailboxError as e:
                return MailRun(error=str(e))
        if not messages:
            self._state.write(newest, now_validity)
            log.info("mail: nothing new")
            return MailRun()
        sorted_, prompt_tokens, output_tokens = await self._classifier.sort(messages)
        self._state.write(newest, now_validity)
        log.info("mail: %d message(s), buckets %s", len(sorted_),
                 ", ".join(sorted({s.bucket for s in sorted_})))
        return MailRun(sorted=sorted_, prompt_tokens=prompt_tokens,
                       output_tokens=output_tokens)

    async def digest(self) -> str:
        return digest(await self.run(), self.buckets)


def messages_of(run: MailRun) -> list[Message]:
    return [s.message for s in run.sorted]
