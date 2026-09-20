"""Web research: Claude with its server-side web search and fetch tools, for a message that asks
to find something and write it down. The interpreter only decides *that* something is to be
looked up (Candidate.web_query); this turns the query into Markdown with sources, which then
goes through the normal write path as the candidate's content."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from itertools import zip_longest
from typing import Any

import anthropic

from app.llm.image_search import ImageSearch
from app.llm.prompts import (
    IMAGE_FILTER_PROMPT,
    IMAGE_QUERIES_PROMPT,
    IMAGES_HEADING,
    RESEARCH_PROMPT,
    RESEARCH_QUESTION,
    SOURCES_HEADING,
    image_filter_message,
    research_message,
)

log = logging.getLogger(__name__)

MAX_CONTINUATIONS = 3  # pause_turn resumes: the server-side tool loop stops every 10 steps
MAX_RESULT_CHARS = 20_000
MAX_FETCH_TOKENS = 8_000  # per fetched page
PREAMBLE_CHARS = 300  # how far into the answer a lead-in before the first heading may run
_FIRST_HEADING = re.compile(r"(?<![\w#])(#{1,3} )")  # "C# " is not a heading
# Models with the dynamic-filtering tool versions; everything else gets the earlier ones.
_DYNAMIC_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
                     "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


MAX_IMAGES = 6
MAX_PHRASES = 3
MAX_SOURCE_PAGES = 4
MAX_CANDIDATES = 16  # pictures shown to the relevance check
KEEP_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["keep"],
               "properties": {"keep": {"type": "array", "items": {"type": "integer"}}}}
# "Letter signed Sara ... (page 5).jpg" and "(page 2)" are one document: compared without the
# page numbers and other digits, its pages collapse into one picture.
_PAGE_MARK = re.compile(r"\(?page\s*\d+\)?|\d+", re.I)
_LINK = re.compile(r"\((https?://[^\s)]+)\)")


class ResearchError(Exception):
    pass


class ResearchQuestion(Exception):
    """The request cannot be looked up as it stands; `question` is for the user."""

    def __init__(self, question: str) -> None:
        super().__init__(question)
        self.question = question


def research_tools(model: str, max_uses: int) -> list[dict]:
    if model.startswith(_DYNAMIC_PREFIXES):
        search, fetch = "web_search_20260209", "web_fetch_20260209"
    else:
        search, fetch = "web_search_20250305", "web_fetch_20250910"
    # A fetched page arrives whole unless capped: one test query read 200k tokens of pages.
    return [{"type": search, "name": "web_search", "max_uses": max_uses},
            {"type": fetch, "name": "web_fetch", "max_uses": max_uses,
             "max_content_tokens": MAX_FETCH_TOKENS}]


def final_text(blocks: list[Any]) -> str:
    """The answer proper: the text after the last tool result. Text before it is the model
    narrating its search ("let me look that up"), which does not belong in the note."""
    last_tool = max((i for i, b in enumerate(blocks) if b.type.endswith("_tool_result")),
                    default=-1)
    text = "".join(b.text for b in blocks[last_tool + 1:] if b.type == "text").strip()
    return _drop_preamble(text)


def _drop_preamble(text: str) -> str:
    """A chatty lead-in ("Great, I found...") before the first heading, despite the prompt. Text
    blocks are joined as they come (a citation can split a sentence), so the lead-in may even
    share the heading's line."""
    m = _FIRST_HEADING.search(text[:PREAMBLE_CHARS])
    return text[m.start(1):].strip() if m and m.start(1) > 0 else text


def _question(text: str) -> str | None:
    """The question in an answer that is nothing but RESEARCH_QUESTION and a question; None
    for a real result."""
    text = text.strip()
    if not text.startswith(RESEARCH_QUESTION):
        return None
    return text[len(RESEARCH_QUESTION):].strip() or None


def _document_key(caption: str) -> str:
    return " ".join(_PAGE_MARK.sub(" ", caption.lower()).split())


def _md_caption(caption: str) -> str:
    """A caption safe inside ![...]: no brackets, one line, not too long."""
    return " ".join(caption.replace("[", "(").replace("]", ")").split())[:120]


def merge(text: str, images: list[str]) -> str:
    """Text and the image lines as one note: the images under their own heading, in front of
    the sources (which close the note) when there are any."""
    if not images:
        return text
    block = "\n\n".join([IMAGES_HEADING, *images])
    if not text:
        return block
    head, sep, tail = text.partition(SOURCES_HEADING)
    if not sep:
        return f"{text}\n\n{block}"
    return f"{head.rstrip()}\n\n{block}\n\n{sep}{tail}"


class WebResearcher:
    def __init__(
        self, api_key: str, model: str, *, max_searches: int = 5, timeout_s: float = 180.0,
        client: anthropic.AsyncAnthropic | None = None,
        is_image: Callable[[str], Awaitable[bool]] | None = None,
        search: ImageSearch | None = None,
    ) -> None:
        self.model = model
        self._tools = research_tools(model, max_searches)
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)
        # Checks an image link before it is kept (ImageHost.is_image); without one, every
        # link the model found is kept and the executor sorts them out at write time.
        self._is_image = is_image
        self._search = search  # where pictures come from; None: text only

    async def aclose(self) -> None:
        await self._client.close()

    async def research(self, request: str, query: str, media: str = "text") -> str:
        """Markdown ready to write: text, pictures, or both, per `media` (the interpreter's
        web_media). The text is Claude's web research; pictures come from Wikimedia Commons
        (Claude only proposes the search phrases) and from the pages the text cites, because
        image links a model writes itself are mostly invented. Raises ResearchQuestion when the
        request needs the user first, ResearchError when nothing usable came back."""
        want_text, want_images = media != "images", media != "text"
        text_call = (self._run(RESEARCH_PROMPT, request, query) if want_text
                     else asyncio.sleep(0, result=""))
        commons_call = (self._commons(request, query) if want_images
                        else asyncio.sleep(0, result=[]))
        text, commons = await asyncio.gather(text_call, commons_call, return_exceptions=True)
        if isinstance(text, ResearchQuestion):
            raise text
        if isinstance(commons, BaseException):
            log.warning("image search failed: %s", commons)
            commons = []
        text_failed = isinstance(text, BaseException)
        images: list[str] = []
        if want_images:
            sources = [] if text_failed else await self._source_images(text)
            images = await self._keep([*commons, *sources], request, query)
        if text_failed:
            if not images:
                raise text
            text = ""  # the pictures alone are still worth writing
        if not text and not images:
            raise ResearchError("no images found" if want_images else "no answer")
        return merge(text, images)

    async def _phrases(self, request: str, query: str) -> list[str]:
        """English search phrases for Commons: one cheap call, no tools."""
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=200, system=IMAGE_QUERIES_PROMPT,
                messages=[{"role": "user", "content": research_message(request, query)}],
            )
        except anthropic.APIError as e:
            log.info("image phrases failed (%s)", getattr(e, "status_code", type(e).__name__))
            return []
        text = "".join(b.text for b in resp.content if b.type == "text")
        lines = [line.strip(" -•\t\"'") for line in text.splitlines()]
        return [line for line in lines if line][:MAX_PHRASES]

    async def _commons(self, request: str, query: str) -> list[tuple[str, str]]:
        if self._search is None:
            return []
        phrases = await self._phrases(request, query)
        found = await asyncio.gather(*(self._commons_one(p) for p in phrases))
        # Interleaved, so each phrase contributes its best picture before any gives a second.
        return [hit for row in zip_longest(*found) for hit in row if hit is not None]

    async def _commons_one(self, phrase: str) -> list[tuple[str, str]]:
        """Commons hits for `phrase`, dropping its last word until something matches: every
        word must match, and one abstract word ("family") empties the whole search."""
        assert self._search is not None
        words = phrase.split()
        while words:
            hits = await self._search.commons(" ".join(words))
            if hits:
                return hits
            words = words[:-1]
        return []

    async def _source_images(self, text: str) -> list[tuple[str, str]]:
        """og:image of the pages the text cites (its sources section)."""
        if self._search is None or SOURCES_HEADING not in text:
            return []
        links = _LINK.findall(text.split(SOURCES_HEADING, 1)[1])[:MAX_SOURCE_PAGES]
        found = await asyncio.gather(*(self._search.og_image(u) for u in links))
        return [hit for hit in found if hit is not None]

    async def _relevant(
        self, request: str, query: str, hits: list[tuple[str, str]]
    ) -> list[tuple[str, str]]:
        """The hits whose captions fit the request, by one cheap call. Commons search also
        matches file descriptions, so "Helsinki Stockholm" once returned six pages of a scanned
        1923 letter. On any failure the hits are kept as they are."""
        if not hits:
            return hits
        listing = "\n".join(f"{i}. {cap or url.rsplit('/', 1)[-1]}"
                            for i, (url, cap) in enumerate(hits, start=1))
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=300, system=IMAGE_FILTER_PROMPT,
                messages=[{"role": "user", "content": image_filter_message(
                    request, query, listing)}],
                output_config={"format": {"type": "json_schema", "schema": KEEP_SCHEMA}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            keep = json.loads(text).get("keep", [])
        except (anthropic.APIError, ValueError, AttributeError) as e:
            log.info("image relevance check skipped (%s)", type(e).__name__)
            return hits
        chosen = {n for n in keep if isinstance(n, int)}
        return [hit for i, hit in enumerate(hits, start=1) if i in chosen]

    async def _keep(
        self, hits: list[tuple[str, str]], request: str = "", query: str = ""
    ) -> list[str]:
        """Image lines for the hits that fit the request and really are images."""
        hits = list({url: (url, cap) for url, cap in hits}.values())  # one line per picture
        hits = list({_document_key(cap) or url: (url, cap)
                     for url, cap in reversed(hits)}.values())[::-1]  # one page per document
        hits = await self._relevant(request, query, hits[:MAX_CANDIDATES])
        if self._is_image is not None:
            ok = await asyncio.gather(*(self._is_image(url) for url, _ in hits))
            hits = [hit for hit, good in zip(hits, ok, strict=True) if good]
        return [f"![{_md_caption(cap)}]({url})" for url, cap in hits[:MAX_IMAGES]]

    async def _run(self, system: str, request: str, query: str) -> str:
        user = {"role": "user", "content": research_message(request, query)}
        blocks: list[Any] = []
        for _ in range(MAX_CONTINUATIONS + 1):
            messages = [user, {"role": "assistant", "content": blocks}] if blocks else [user]
            try:
                resp = await self._client.messages.create(
                    model=self.model, max_tokens=4096, system=system,
                    messages=messages, tools=self._tools,
                )
            except anthropic.APIError as e:
                status = getattr(e, "status_code", None)
                raise ResearchError(f"claude {status or type(e).__name__}") from None
            blocks = [*blocks, *resp.content]
            if resp.stop_reason != "pause_turn":
                break
        if resp.stop_reason == "refusal":
            raise ResearchError("claude declined")
        text = final_text(blocks)
        question = _question(text)
        if question:
            raise ResearchQuestion(question)
        if not text:
            raise ResearchError(f"no answer (stop_reason={resp.stop_reason})")
        log.info("llm %s researched the web: %d chars | %d+%d tok", self.model, len(text),
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return text[:MAX_RESULT_CHARS]
