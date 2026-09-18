"""Primary interpreter with a fallback: Claude first, the local model when Claude cannot answer.

The resolver validates whichever answer comes back the same way, so the switch changes quality
and speed, never what the bot is allowed to do."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from app.interpretation.models import Interpretation
from app.llm.base import LLMClient, LLMError, LLMTrace, LLMUnavailable
from app.llm.context import Context

log = logging.getLogger(__name__)

# After the primary fails for lack of service (offline, rate-limited, no credit, bad key), skip
# it for this long: each message would otherwise wait for the same failure before falling back.
DEFAULT_COOLDOWN_S = 300.0


class FallbackLLM:
    def __init__(
        self,
        primary: LLMClient,
        fallback: LLMClient,
        *,
        cooldown_s: float = DEFAULT_COOLDOWN_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.primary = primary
        self.fallback = fallback
        self._cooldown_s = cooldown_s
        self._clock = clock
        self._skip_until = 0.0

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        if self._clock() >= self._skip_until:
            try:
                interp, trace = await self.primary.interpret(text, context, schema)
            except LLMUnavailable as e:
                self._skip_until = self._clock() + self._cooldown_s
                log.warning("primary LLM unavailable (%s); local model for the next %.0f s",
                            e, self._cooldown_s)
            except LLMError as e:  # an answer that failed validation: this message only
                log.warning("primary LLM gave no usable answer (%s); asking the local model", e)
            else:
                # The primary never saw a local-only target's items or description, so an answer
                # naming one may have guessed exactly the details it lacked: the local model,
                # which sees them, reads the message again.
                if not any(c.target in context.local_only for c in interp.candidates):
                    return interp, trace
                log.info("primary LLM chose a local-only target; re-reading locally")
        return await self.fallback.interpret(text, context, schema)

    async def models(self) -> list[str]:
        """Both clients' models. A primary that cannot be reached is logged, not raised: the
        bot still runs, on the local model."""
        names: list[str] = []
        try:
            names += await self.primary.models()
        except LLMError as e:
            log.warning("primary LLM check failed (%s); the local model will answer", e)
        return names + await self.fallback.models()

    async def aclose(self) -> None:
        for client in (self.primary, self.fallback):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()
