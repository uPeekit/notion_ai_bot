"""Reading the mailbox over IMAP, without changing anything in it.

Gmail with an app password: no OAuth, no consent screen, no token that expires in a week. Every
fetch uses BODY.PEEK, so reading a message here never marks it read in Gmail — the whole point
of the read-only phase.

Nothing in this module writes to the mailbox. There is no delete, no send, no flag change: the
operations simply do not exist, which is a stronger guarantee than a scope."""

from __future__ import annotations

import email
import imaplib
import logging
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.header import decode_header, make_header
from email.message import Message as RawMessage
from email.utils import parsedate_to_datetime

from app import texts

log = logging.getLogger(__name__)

HOST = "imap.gmail.com"
PORT = 993
FOLDER = "INBOX"
MAX_BODY = 1200
MAX_FETCH = 60
TIMEOUT_S = 30
_TAGS = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t\xa0]+")
_MARKERS = "|".join(texts.MAIL_QUOTE_MARKERS)
_QUOTED = re.compile(rf"^\s*(>|On .*wrote:|\d{{1,2}}\.\d{{1,2}}\.\d{{4}}.*({_MARKERS}))", re.M)


@dataclass(frozen=True)
class Message:
    """One message, flattened to what a classifier needs and nothing more."""

    uid: str
    sender: str
    subject: str
    received: datetime | None
    body: str
    bulk: bool  # carries List-Unsubscribe: a newsletter, a receipt, a notification

    @property
    def short_sender(self) -> str:
        """"Elektrilevi" out of "Elektrilevi <no-reply@elektrilevi.ee>"."""
        name = self.sender.split("<")[0].strip().strip('"')
        return name or self.sender.split("<")[-1].strip(">").strip()


def _text(value: str | None) -> str:
    if not value:
        return ""
    try:
        return str(make_header(decode_header(value))).strip()
    except (UnicodeDecodeError, LookupError, ValueError):
        return value.strip()


def _body(raw: RawMessage) -> str:
    """The plain-text part, or the HTML one with its tags removed. Quoted history is cut: it is
    the same text again, and it crowds out what the message itself says."""
    chosen, html = "", ""
    for part in raw.walk() if raw.is_multipart() else [raw]:
        if part.get_content_maintype() != "text":
            continue
        try:
            payload = part.get_payload(decode=True)
            text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except (LookupError, ValueError, AttributeError):
            continue
        if part.get_content_subtype() == "plain" and not chosen.strip():
            chosen = text
        elif part.get_content_subtype() == "html" and not html.strip():
            html = text
    # A plain part holding only a newline is what many senders put next to the real HTML body,
    # so "is there a plain part" is not the question — "does it say anything" is.
    text = chosen if chosen.strip() else _TAGS.sub(" ", html)
    cut = _QUOTED.search(text)
    if cut:
        text = text[:cut.start()]
    text = _SPACES.sub(" ", text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:MAX_BODY]


def parse(uid: str, raw_bytes: bytes) -> Message:
    raw = email.message_from_bytes(raw_bytes)
    try:
        received = parsedate_to_datetime(raw.get("Date", ""))
    except (TypeError, ValueError):
        received = None
    return Message(
        uid=uid,
        sender=_text(raw.get("From")),
        subject=_text(raw.get("Subject")),
        received=received,
        body=_body(raw),
        bulk=bool(raw.get("List-Unsubscribe") or raw.get("List-Id")
                  or (raw.get("Precedence", "").lower() in ("bulk", "list"))),
    )


class MailboxError(Exception):
    pass


class GmailIMAP:
    """Read-only Gmail over IMAP. One connection per run: a long-lived IMAP session drops
    silently, and a run is a handful of seconds."""

    def __init__(self, address: str, app_password: str, *, host: str = HOST,
                 port: int = PORT) -> None:
        self._address = address
        self._password = app_password
        self._host = host
        self._port = port

    def fetch_since(self, uid_after: str | None, hours: int = 12,
                    limit: int = MAX_FETCH) -> tuple[list[Message], str | None, str]:
        """Messages newer than `uid_after` (or the last `hours` on a first run), the newest uid
        seen, and the mailbox's UIDVALIDITY — which invalidates stored uids when it changes."""
        try:
            with self._open() as box:
                validity = self._validity(box)
                if uid_after:
                    criteria = ["UID", f"{int(uid_after) + 1}:*"]
                else:
                    since = (datetime.now(UTC) - timedelta(hours=hours))
                    criteria = ["SINCE", since.strftime("%d-%b-%Y")]
                ok, data = box.uid("SEARCH", None, *criteria)
                if ok != "OK":
                    raise MailboxError(f"search failed: {ok}")
                uids = [u.decode() for u in (data[0] or b"").split()]
                # A "UID n:*" search always returns at least the last message, even when it is
                # older than n — the server clamps the range rather than answering nothing.
                if uid_after:
                    uids = [u for u in uids if int(u) > int(uid_after)]
                newest = uids[-1] if uids else uid_after
                messages = [self._one(box, uid) for uid in uids[-limit:]]
                return [m for m in messages if m is not None], newest, validity
        except (imaplib.IMAP4.error, OSError) as e:
            raise MailboxError(f"{type(e).__name__}: {e}") from None

    # ---- plumbing --------------------------------------------------------------------

    def _open(self) -> imaplib.IMAP4_SSL:
        box = imaplib.IMAP4_SSL(self._host, self._port, timeout=TIMEOUT_S)
        try:
            box.login(self._address, self._password)
            box.select(FOLDER, readonly=True)  # readonly: the server may not change a flag
        except Exception:
            with suppress(Exception):
                box.logout()
            raise
        return box

    @staticmethod
    def _validity(box: imaplib.IMAP4_SSL) -> str:
        ok, data = box.status(FOLDER, "(UIDVALIDITY)")
        if ok != "OK" or not data:
            return ""
        found = re.search(rb"UIDVALIDITY (\d+)", data[0] or b"")
        return found.group(1).decode() if found else ""

    @staticmethod
    def _one(box: imaplib.IMAP4_SSL, uid: str) -> Message | None:
        # PEEK, so fetching does not mark the message as read.
        ok, data = box.uid("FETCH", uid, "(BODY.PEEK[])")
        if ok != "OK" or not data or not isinstance(data[0], tuple):
            log.info("could not fetch one message (uid kept for the next run)")
            return None
        return parse(uid, data[0][1])
