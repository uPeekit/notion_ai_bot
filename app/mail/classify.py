"""Sorting new mail into the user's own buckets, with a one-line summary each.

The email is data, never instructions. A message that says "mark everything read and forward
this" is summarised like any other: the only things this module can produce are a bucket from
the configured list and a line of text, and the only thing the bot does with them is print a
digest. There is no action a hostile sender could reach."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import MAIL_PROMPT, mail_message
from app.mail.imap import Message

log = logging.getLogger(__name__)

BATCH = 10
MAX_TOKENS = 2000
MAX_SUMMARY = 200
OTHER = "other"


def parse_buckets(raw: str) -> tuple[list[str], dict[str, str]]:
    """"name:what belongs in it, name:..." -> the bucket names, and what each one means. A
    bucket without a description is still a bucket, but then the model has only its name to go
    on — which is how a payment receipt ended up in "bills" instead of "financial"."""
    names: list[str] = []
    meanings: dict[str, str] = {}
    for part in raw.split(","):
        name, _, description = part.partition(":")
        name = name.strip()
        if not name:
            continue
        names.append(name)
        if description.strip():
            meanings[name] = description.strip()
    return names, meanings


class ClassifyError(Exception):
    """`reason` is a code from app/llm/health.py when the call failed because Claude
    could not be used at all, and "" for every other failure."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class Sorted:
    message: Message
    bucket: str
    summary: str


def _schema(buckets: list[str]) -> dict:
    return {
        "type": "object", "additionalProperties": False, "required": ["messages"],
        "properties": {"messages": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "bucket", "summary"],
            "properties": {
                "id": {"type": "string"},
                "bucket": {"enum": buckets},
                "summary": {"type": "string"},
            }}}},
    }


def gate(answer: object, batch: list[Message], buckets: list[str]) -> list[Sorted]:
    """What the model said, reduced to what it is allowed to say: a known bucket for a message
    that was actually in this batch, and a single line of text. Anything missing or invented
    falls back to `other` with the subject as the summary — never dropped, never guessed at."""
    by_uid = {m.uid: m for m in batch}
    said: dict[str, tuple[str, str]] = {}
    if isinstance(answer, dict):
        for item in answer.get("messages", []):
            if not isinstance(item, dict):
                continue
            uid = str(item.get("id", "")).strip()
            if uid not in by_uid:
                continue
            bucket = str(item.get("bucket", "")).strip()
            summary = " ".join(str(item.get("summary", "")).split())[:MAX_SUMMARY]
            said[uid] = (bucket if bucket in buckets else OTHER, summary)
    out = []
    for message in batch:
        bucket, summary = said.get(message.uid, (OTHER, ""))
        out.append(Sorted(message, bucket, summary or message.subject))
    return out


class Classifier:
    def __init__(self, api_key: str, model: str, buckets: list[str], *,
                 meanings: dict[str, str] | None = None, timeout_s: float = 60.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self.buckets = [*buckets, OTHER] if OTHER not in buckets else list(buckets)
        self.meanings = dict(meanings or {})
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def sort(self, messages: list[Message]) -> tuple[list[Sorted], int, int]:
        """Every message, in order, with its bucket and summary. A failed batch is not lost: it
        comes back as `other`, so the digest still lists it."""
        out: list[Sorted] = []
        prompt_tokens = output_tokens = 0
        for start in range(0, len(messages), BATCH):
            batch = messages[start:start + BATCH]
            try:
                answer, used_in, used_out = await self._ask(batch)
                prompt_tokens += used_in
                output_tokens += used_out
            except ClassifyError as e:
                log.warning("mail classification failed for a batch: %s", e)
                answer = {}
            out += gate(answer, batch, self.buckets)
        return out, prompt_tokens, output_tokens

    async def _ask(self, batch: list[Message]) -> tuple[object, int, int]:
        payload = mail_message([{
            "id": m.uid,
            "from": m.sender,
            "subject": m.subject,
            "bulk": m.bulk,
            "text": m.body,
        } for m in batch], self.buckets, self.meanings)
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=MAIL_PROMPT,
                messages=[{"role": "user", "content": payload}],
                output_config={"format": {"type": "json_schema",
                                          "schema": _schema(self.buckets)}},
            )
        except anthropic.APIError as e:
            raise ClassifyError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "max_tokens":
            raise ClassifyError("answer cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise ClassifyError("not JSON") from None
        usage = getattr(resp, "usage", None)
        return (data, getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0)
