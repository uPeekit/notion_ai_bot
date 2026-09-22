"""Links between notes, which is what a vault is for.

Two passes over a note the bot has just written. The first is free and certain: a name or alias
of an existing note, standing on its own in the text, becomes a [[link]]. The second asks the
small model for the links that matching cannot see — another word form, a person by nickname,
the project a line belongs to — and accepts an answer only when the phrase really is in the
text and the note really exists. Neither pass creates or rewrites anything: they only link.

It runs after the reply has gone out, so it never makes the chat slower."""

from __future__ import annotations

import json
import logging
import re

import anthropic

from app import texts
from app.vault.frontmatter import split
from app.vault.index import MIN_WORD, VaultIndex, stems
from app.vault.writer import VaultWrite, VaultWriter

log = logging.getLogger(__name__)

MAX_LINKS = 10
MAX_CANDIDATES = 50
MAX_TOKENS = 1000
MAX_TEXT = 4000
# Notes that are lists of lines rather than prose: linking every word there is noise.
SKIP_NOTES = (texts.VAULT_TASKS_NOTE, texts.VAULT_ARCHIVE_NOTE, texts.VAULT_INBOX_NOTE)
_STRING = {"type": "string"}
LINK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["links"],
    "properties": {"links": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["phrase", "note"],
        "properties": {"phrase": _STRING, "note": _STRING}}}},
}
# A place in the text where a link may not be put: an existing link, a markdown link's target,
# a tag, a fenced code block, an inline code span, a URL, or a property line.
_PROTECTED = re.compile(
    r"\[\[[^\]]*\]\]|\[[^\]]*\]\([^)]*\)|`[^`]*`|```.*?```|https?://\S+|(?:^|\s)#[\w/-]+",
    re.DOTALL,
)


def _free_spans(text: str) -> list[tuple[int, int]]:
    spans, at = [], 0
    for m in _PROTECTED.finditer(text):
        if m.start() > at:
            spans.append((at, m.start()))
        at = m.end()
    if at < len(text):
        spans.append((at, len(text)))
    return spans


def _find(text: str, phrase: str) -> tuple[int, int] | None:
    """Where `phrase` stands as a whole word, outside anything already marked up."""
    pattern = re.compile(rf"(?<![\w-]){re.escape(phrase)}(?![\w-])", re.IGNORECASE)
    for start, end in _free_spans(text):
        m = pattern.search(text, start, end)
        if m:
            return m.span()
    return None


def apply_links(body: str, pairs: list[tuple[str, str]], limit: int = MAX_LINKS) -> str:
    """`body` with each (phrase, note) pair linked once, longest phrase first so a longer name
    is not eaten by a shorter one inside it."""
    added = 0
    for phrase, note in sorted(pairs, key=lambda p: -len(p[0])):
        if added >= limit or not phrase.strip() or not note.strip():
            continue
        span = _find(body, phrase)
        if span is None:
            continue
        start, end = span
        seen = body[start:end]
        link = f"[[{note}]]" if seen.casefold() == note.casefold() else f"[[{note}|{seen}]]"
        body = body[:start] + link + body[end:]
        added += 1
    return body


def obvious_links(body: str, index: VaultIndex, *, exclude: str = "") -> list[tuple[str, str]]:
    """What plain matching already knows: a note's name or alias present in the text."""
    text_stems = stems(body)
    out: list[tuple[str, str]] = []
    for note in index.notes:
        if note.name == exclude or note.name.startswith("_"):
            continue
        for name in note.names:
            if len(name) < MIN_WORD or not stems(name) & text_stems:
                continue
            if _find(body, name) is not None:
                out.append((name, note.name))
                break
    return out


class Linker:
    def __init__(self, index: VaultIndex, writer: VaultWriter, *, api_key: str = "",
                 model: str = "", client: anthropic.AsyncAnthropic | None = None,
                 timeout_s: float = 30.0) -> None:
        self._index = index
        self._writer = writer
        self.model = model
        self._client = client
        if client is None and api_key and model:
            self._client = anthropic.AsyncAnthropic(api_key=api_key, timeout=timeout_s,
                                                     max_retries=0)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.close()

    async def link(self, write: VaultWrite) -> int:
        """Link the note this write touched. Returns how many links were added; never raises —
        a note without links is a small loss, a failed message is not."""
        if write.note in SKIP_NOTES or write.kind == "inbox":
            return 0
        try:
            text = self._writer.read(write.path)
        except OSError:
            return 0
        props, body = split(text)
        pairs = obvious_links(body, self._index, exclude=write.note)
        pairs += await self._model_links(body, write.note, [p for p, _ in pairs])
        linked = apply_links(body, pairs)
        if linked == body:
            return 0
        added = linked.count("[[") - body.count("[[")
        self._writer.replace(write.path, text.replace(body, linked, 1))
        log.info("linker added %d link(s) to %s", added, write.note)
        return added

    def _candidates(self, body: str, note: str) -> list[str]:
        """Which notes the model may link to. Word overlap first — but overlap is exactly what
        this pass exists to get past, so the most recently touched notes fill the rest of the
        list: a person or project written about lately is what a new note usually refers to."""
        names: list[str] = []
        for candidate in self._index.candidates(body, MAX_CANDIDATES):
            if candidate.name != note:
                names.append(candidate.name)
        recent = sorted(self._index.notes, key=lambda n: -n.mtime)
        for candidate in recent:
            if len(names) >= MAX_CANDIDATES:
                break
            if (candidate.name != note and candidate.name not in names
                    and candidate.name not in SKIP_NOTES
                    and not candidate.name.startswith("_")):
                names.append(candidate.name)
        return names

    async def _model_links(self, body: str, note: str,
                           already: list[str]) -> list[tuple[str, str]]:
        if self._client is None or not body.strip():
            return []
        candidates = self._candidates(body, note)
        if not candidates:
            return []
        from app.llm.prompts import LINKER_PROMPT, linker_message

        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=LINKER_PROMPT,
                messages=[{"role": "user", "content": linker_message(
                    body[:MAX_TEXT], candidates, already)}],
                output_config={"format": {"type": "json_schema", "schema": LINK_SCHEMA}},
            )
            data = json.loads(next((b.text for b in resp.content if b.type == "text"), "{}"))
        except (anthropic.APIError, json.JSONDecodeError, ValueError) as e:
            log.info("linker model call failed (%s)", type(e).__name__)
            return []
        known = {c.casefold(): c for c in candidates}
        out: list[tuple[str, str]] = []
        for item in data.get("links", [])[:MAX_LINKS]:
            if not isinstance(item, dict):
                continue
            phrase, target = str(item.get("phrase", "")), str(item.get("note", "")).strip("[] ")
            # The model may only point at a note that exists, with words that are really there.
            if known.get(target.casefold()) and _find(body, phrase) is not None:
                out.append((phrase, known[target.casefold()]))
        return out
