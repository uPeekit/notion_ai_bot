"""Which section of a page a line joins.

The interpreter never sees a page's contents — only its sub-pages — so it cannot say that a
film belongs under "to watch" rather than under "podcasts". The executor has just read the
page to fit the line to a list, so by then the headings are known; this asks the model to pick
one, and only when there is really something to choose between (two or more sections that end
in a list). One short call, no tools, a handful of tokens.

A page whose target is local_only never gets here: the executor is given no request text for
it, and without that the line goes to the end of the page as before.
"""

from __future__ import annotations

import json
import logging

import anthropic

from app.llm.health import Health
from app.llm.prompts import SECTION_PROMPT, section_message

log = logging.getLogger(__name__)

MAX_SECTIONS = 12  # a page with more than this is not a list of sections any more
MAX_TOKENS = 64
PICK_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["section"],
    "properties": {"section": {"type": "integer"}},
}


class SectionPicker:
    def __init__(
        self, api_key: str, model: str, *, timeout_s: float = 20.0,
        client: anthropic.AsyncAnthropic | None = None,
        health: Health | None = None,
    ) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def pick(self, request: str, line: str, headings: list[str]) -> int | None:
        """The index in `headings` of the section the line belongs in, or None — nothing fits,
        too many sections, or the call failed. None always means "as before", never an error:
        the line is still written, just at the end of the page."""
        if len(headings) < 2 or len(headings) > MAX_SECTIONS:
            return None
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=SECTION_PROMPT,
                messages=[{"role": "user",
                           "content": section_message(request, line, headings)}],
                output_config={"format": {"type": "json_schema", "schema": PICK_SCHEMA}},
            )
            text = next((b.text for b in resp.content if b.type == "text"), "")
            chosen = json.loads(text).get("section")
        except (anthropic.APIError, ValueError, AttributeError) as e:
            if isinstance(e, anthropic.APIError):
                self._health.record(e)
            log.info("section pick skipped (%s)", type(e).__name__)
            return None
        if not isinstance(chosen, int) or not 1 <= chosen <= len(headings):
            return None
        log.info("llm %s put the line under section %d of %d", self.model, chosen,
                 len(headings))
        return chosen - 1
