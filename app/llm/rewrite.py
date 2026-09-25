"""Rewriting text the user already has: one model call, and nothing else.

Both stores use it. The Notion side hands over a page's blocks turned into markdown, the
Obsidian side hands over a note or one of its sections; what comes back is markdown, and each
store writes it back its own way. Nothing here reads or writes anything — deciding what may be
replaced is the caller's job, and in the Notion case a deterministic one (see
app/notion/to_markdown.py: an image is never part of the text being rewritten).

Sonnet rather than Haiku on purpose: this is the user's own writing being consolidated, which
is the one job in the bot where a cheaper model is obviously worse."""

from __future__ import annotations

import logging

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import REWRITE_PROMPT, rewrite_message

log = logging.getLogger(__name__)

# What one rewrite may read and write. The input cap is the page size past which the reply says
# "one section at a time" instead of quietly truncating someone's page.
MAX_INPUT = 20_000
MAX_TOKENS = 8_000
# A model that answers with the text wrapped in a fence, despite being told not to. A fence
# with any other language after it is the page's own code block, not a wrapper.
_FENCES = ("```markdown\n", "```md\n", "```\n")


class RewriteError(Exception):
    """`reason` is a code from app/llm/health.py when Claude could not be used at all."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


def unfence(text: str) -> str:
    """The answer without the code fence a model sometimes wraps the whole thing in. Only when
    it wraps *everything*: a page that really is one code block keeps its fence."""
    body = text.strip()
    for fence in _FENCES:
        if body.startswith(fence) and body.endswith("```") and len(body) > len(fence) + 3:
            inner = body[len(fence):-3].strip("\n")
            if "```" not in inner:
                return inner
    return body


class Rewriter:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 120.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def rewrite(self, current: str, instruction: str) -> tuple[str, int, int]:
        """The new text, and what it cost. Raises RewriteError — never returns an empty string,
        because "the model said nothing" must not be mistaken for "the page should be empty"."""
        if not current.strip():
            raise RewriteError("nothing to rewrite")
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=REWRITE_PROMPT,
                messages=[{"role": "user",
                           "content": rewrite_message(current[:MAX_INPUT], instruction)}],
            )
        except anthropic.APIError as e:
            raise RewriteError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "refusal":
            raise RewriteError("claude declined")
        text = unfence("".join(b.text for b in resp.content if b.type == "text"))
        if not text.strip():
            raise RewriteError("empty answer")
        if resp.stop_reason == "max_tokens":
            # Half a page is worse than none: the tail would simply disappear.
            raise RewriteError("answer cut off (max_tokens)")
        log.info("rewrote %d chars into %d with %s", len(current), len(text), self.model)
        return text, resp.usage.input_tokens, resp.usage.output_tokens
