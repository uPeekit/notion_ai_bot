from __future__ import annotations

import time

import httpx
from pydantic import ValidationError

from app.interpretation.models import Interpretation
from app.llm.base import LLMContextOverflow, LLMInvalidOutput, LLMTrace, LLMUnavailable
from app.llm.context import Context
from app.llm.prompts import build_messages, retry_message

THINKING_FAMILIES = ("qwen3", "deepseek-r1", "gpt-oss", "magistral")
MAX_ATTEMPTS = 2
CTX_MARGIN = 256


def wants_think_flag(model: str) -> bool:
    family = model.split(":", 1)[0].lower()
    return family in THINKING_FAMILIES


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.0,
        num_ctx: int = 16384,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._temperature = temperature
        self._num_ctx = num_ctx
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), timeout=timeout_s, transport=transport
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def models(self) -> list[str]:
        data = await self._call("GET", "/api/tags")
        return [m["name"] for m in data.get("models", []) if "name" in m]

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        base_messages = build_messages(text, context)
        messages = base_messages
        start = time.monotonic()
        raw = ""
        error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            body: dict = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "options": {"temperature": self._temperature, "num_ctx": self._num_ctx},
            }
            if wants_think_flag(self.model):
                body["think"] = False
            data = await self._call("POST", "/api/chat", body)
            raw = data.get("message", {}).get("content") or ""
            prompt_tokens = data.get("prompt_eval_count")
            output_tokens = data.get("eval_count")
            done_reason = data.get("done_reason")
            if prompt_tokens is not None and prompt_tokens >= self._num_ctx - CTX_MARGIN:
                # Ollama silently drops the head of the prompt past num_ctx; the answer is
                # untrustworthy, so surface it distinctly instead of retrying or returning it.
                raise LLMContextOverflow(
                    f"context overflow: prompt_tokens={prompt_tokens} num_ctx={self._num_ctx}",
                    prompt_tokens=prompt_tokens,
                    num_ctx=self._num_ctx,
                )
            if done_reason == "length":
                error = "output truncated (num_predict)"
            else:
                try:
                    interp = Interpretation.model_validate_json(raw)
                except ValidationError as e:
                    error = str(e)[:800]
                else:
                    return interp, LLMTrace(
                        model=self.model,
                        messages=messages,
                        raw_response=raw,
                        duration_ms=int((time.monotonic() - start) * 1000),
                        attempts=attempt,
                        prompt_tokens=prompt_tokens,
                        output_tokens=output_tokens,
                        done_reason=done_reason,
                    )
            messages = [*base_messages, {"role": "user", "content": retry_message(error)}]
        raise LLMInvalidOutput(
            f"invalid LLM output after {MAX_ATTEMPTS} attempts: {error}", raw=raw
        )

    async def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"ollama unreachable: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise LLMUnavailable(f"ollama {resp.status_code}: {resp.text[:200]}")
        try:
            return resp.json()
        except ValueError:
            raise LLMUnavailable("ollama returned non-JSON") from None
