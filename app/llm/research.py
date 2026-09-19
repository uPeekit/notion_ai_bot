"""Web research: Claude with its server-side web search and fetch tools, for a message that asks
to find something and write it down. The interpreter only decides *that* something is to be
looked up (Candidate.web_query); this turns the query into Markdown with sources, which then
goes through the normal write path as the candidate's content."""

from __future__ import annotations

import logging
import re
from typing import Any

import anthropic

from app.llm.prompts import RESEARCH_PROMPT, research_message

log = logging.getLogger(__name__)

MAX_CONTINUATIONS = 3  # pause_turn resumes: the server-side tool loop stops every 10 steps
MAX_RESULT_CHARS = 20_000
MAX_FETCH_TOKENS = 8_000  # per fetched page
PREAMBLE_CHARS = 300  # how far into the answer a lead-in before the first heading may run
_FIRST_HEADING = re.compile(r"(?<![\w#])(#{1,3} )")  # "C# " is not a heading
# Models with the dynamic-filtering tool versions; everything else gets the earlier ones.
_DYNAMIC_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-mythos-5",
                     "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-sonnet-4-6")


class ResearchError(Exception):
    pass


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


class WebResearcher:
    def __init__(
        self, api_key: str, model: str, *, max_searches: int = 5, timeout_s: float = 180.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._tools = research_tools(model, max_searches)
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def research(self, request: str, query: str) -> str:
        """Markdown ready to write. Raises ResearchError when nothing usable came back."""
        user = {"role": "user", "content": research_message(request, query)}
        blocks: list[Any] = []
        for _ in range(MAX_CONTINUATIONS + 1):
            messages = [user, {"role": "assistant", "content": blocks}] if blocks else [user]
            try:
                resp = await self._client.messages.create(
                    model=self.model, max_tokens=4096, system=RESEARCH_PROMPT,
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
        if not text:
            raise ResearchError(f"no answer (stop_reason={resp.stop_reason})")
        log.info("web research: %d chars, %d input / %d output tokens", len(text),
                 resp.usage.input_tokens, resp.usage.output_tokens)
        return text[:MAX_RESULT_CHARS]
