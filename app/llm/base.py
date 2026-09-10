from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.interpretation.models import Interpretation
from app.llm.context import Context


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMInvalidOutput(LLMError):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


class LLMContextOverflow(LLMError):
    """Ollama silently drops the head of the prompt once it exceeds num_ctx; the answer is
    untrustworthy and must not be treated as valid output."""

    def __init__(self, message: str, prompt_tokens: int, num_ctx: int) -> None:
        super().__init__(message)
        self.prompt_tokens = prompt_tokens
        self.num_ctx = num_ctx


@dataclass
class LLMTrace:
    model: str
    messages: list[dict]
    raw_response: str
    duration_ms: int
    attempts: int
    prompt_tokens: int | None = None
    output_tokens: int | None = None
    done_reason: str | None = None


class LLMClient(Protocol):
    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]: ...

    async def models(self) -> list[str]: ...
