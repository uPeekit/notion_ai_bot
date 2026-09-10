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


@dataclass
class LLMTrace:
    model: str
    messages: list[dict]
    raw_response: str
    duration_ms: int
    attempts: int


class LLMClient(Protocol):
    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]: ...

    async def models(self) -> list[str]: ...
