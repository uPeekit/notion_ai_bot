"""Claude (Anthropic API) as the interpreter. Same contract as `OllamaClient`: one structured
answer per message, validated by `Interpretation`, one corrective retry. Every API failure —
network, rate limit, auth, credit, overload — surfaces as `LLMUnavailable`, which is what the
fallback wrapper switches to the local model on."""

from __future__ import annotations

import copy
import time
from typing import Any

import anthropic
from pydantic import ValidationError

from app.interpretation.models import Interpretation
from app.llm.base import LLMInvalidOutput, LLMTrace, LLMUnavailable
from app.llm.context import Context
from app.llm.prompts import build_messages, retry_message

MAX_ATTEMPTS = 2
# Structured outputs reject these keywords; `Interpretation` (and the resolver after it) check
# what they expressed — confidence bounds, candidate counts — on the answer instead.
UNSUPPORTED_KEYWORDS = frozenset(
    {"minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
     "minLength", "maxLength", "minItems", "maxItems", "uniqueItems"}
)


def api_schema(schema: dict) -> dict:
    """The output schema as the structured-outputs API accepts it: unsupported constraints
    removed, and every array given an `items` (the "no items at all" array we build as
    `maxItems: 0` would otherwise lose its only constraint)."""

    def clean(node: Any) -> Any:
        if isinstance(node, list):
            return [clean(n) for n in node]
        if not isinstance(node, dict):
            return node
        out = {k: clean(v) for k, v in node.items() if k not in UNSUPPORTED_KEYWORDS}
        if out.get("type") == "array" and "items" not in out:
            out["items"] = {"type": "string"}
        return out

    return clean(copy.deepcopy(schema))


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 30.0,
        max_tokens: int = 4096,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        # One SDK retry covers a blip; anything longer is the fallback's job, not a wait here.
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1
        )

    async def aclose(self) -> None:
        await self._client.close()

    async def __aenter__(self) -> ClaudeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def models(self) -> list[str]:
        """The configured model if the key can reach it — a free call, so the startup check
        catches a wrong key or model name before the first message does."""
        try:
            info = await self._client.models.retrieve(self.model)
        except anthropic.APIError as e:
            raise LLMUnavailable(_describe(e)) from None
        return [self.model, info.id]

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        base = build_messages(text, context, cloud=True)
        system = base[0]["content"]
        messages: list[dict] = base[1:]
        output_config = {"format": {"type": "json_schema", "schema": api_schema(schema)}}
        start = time.monotonic()
        raw = ""
        error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                resp = await self._client.messages.create(
                    model=self.model,
                    max_tokens=self._max_tokens,
                    system=system,
                    messages=messages,
                    output_config=output_config,
                )
            except anthropic.APIError as e:
                raise LLMUnavailable(_describe(e)) from None
            raw = next((b.text for b in resp.content if b.type == "text"), "")
            if resp.stop_reason == "refusal":
                raise LLMInvalidOutput("claude declined to answer", raw=raw)
            if resp.stop_reason == "max_tokens":
                error = "output truncated (max_tokens)"
            else:
                try:
                    interp = Interpretation.model_validate_json(raw)
                except ValidationError as e:
                    error = str(e)[:800]
                else:
                    return interp, LLMTrace(
                        model=self.model,
                        messages=[base[0], *messages],
                        raw_response=raw,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        attempts=attempt,
                        prompt_tokens=resp.usage.input_tokens,
                        output_tokens=resp.usage.output_tokens,
                        done_reason=resp.stop_reason,
                    )
            messages = [
                *base[1:],
                {"role": "assistant", "content": raw or "{}"},
                {"role": "user", "content": retry_message(error)},
            ]
        raise LLMInvalidOutput(
            f"invalid LLM output after {MAX_ATTEMPTS} attempts: {error}", raw=raw
        )


def _describe(e: anthropic.APIError) -> str:
    """Status and the API's own error message — never the request, which carries the key."""
    if isinstance(e, anthropic.APITimeoutError):
        return "claude timed out"
    if isinstance(e, anthropic.APIConnectionError):
        return "claude unreachable"
    status = getattr(e, "status_code", None)
    return f"claude {status}: {getattr(e, 'message', type(e).__name__)[:200]}"
