"""Claude (Anthropic API) as the interpreter. Same contract as `OllamaClient`: one structured
answer per message, validated against the request's own schema, one corrective retry. Every API
failure — network, rate limit, auth, credit, overload — surfaces as `LLMUnavailable`, which is
what the fallback wrapper switches to the local model on.

Claude's structured outputs compile a schema with at most 16 union-typed parameters; ours has
one branch per target and four per field (84 on a real workspace). So Claude answers in a flat
shape without unions — fields as a list of {key, status, value_json}, "" for null — which
`to_interpretation` turns back into the usual answer, and `jsonschema` then checks that against
the full per-request schema: the same guarantees the local model's grammar gives."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic
import jsonschema
from pydantic import ValidationError

from app.interpretation.models import WEB_MEDIA, Interpretation
from app.llm.base import LLMInvalidOutput, LLMTrace, LLMUnavailable
from app.llm.context import Context
from app.llm.health import Health, describe
from app.llm.output_schema import intents
from app.llm.prompts import FLAT_FORMAT_NOTE, build_messages, retry_message

MAX_ATTEMPTS = 2
STATUSES = ["value", "ambiguous", "explicit_null", "not_mentioned"]
LIST_TYPES = frozenset({"multi_select", "relation"})


def _obj(props: dict) -> dict:
    return {"type": "object", "properties": props, "required": list(props),
            "additionalProperties": False}


def flat_schema(ctx: Context) -> dict:
    """The answer shape Claude is constrained to: no unions at all."""
    string, number = {"type": "string"}, {"type": "number"}
    field_keys = [fk for tk in ctx.target_keys() for fk in ctx.field_keys(tk)]
    field = _obj({
        "key": {"enum": field_keys} if field_keys else string,
        "status": {"enum": STATUSES},
        "value_json": string,
        "confidence": number,
        "source_text": string,
    })
    candidate = _obj({
        "target_name": string,
        "target": {"enum": ctx.target_keys()},
        "confidence": number,
        "item": string,
        "item_candidates": {"type": "array", "items": string},
        "item_text": string,
        "fields": {"type": "array", "items": field},
        "content": string,
        "search_query": string,
        **({"web_query": string, "web_media": {"enum": list(WEB_MEDIA)}}
           if ctx.web_research else {}),
    })
    return _obj({
        "notes": string,
        "clarify": string,
        "intent": _obj({"value": {"enum": intents(ctx)}, "confidence": number}),
        "candidates": {"type": "array", "items": candidate},
    })


def to_interpretation(flat: dict, ctx: Context) -> dict:
    """The flat answer in the shape `build_schema` describes. Raises ValueError on a value_json
    that is not JSON; everything else is left for the schema check to judge."""

    def text_or_none(v: Any) -> Any:
        return None if v == "" else v

    candidates = []
    for c in flat.get("candidates", []):
        tk = c.get("target")
        fields: dict[str, dict] = {fk: {"status": "not_mentioned"} for fk in ctx.field_keys(tk)}
        for f in c.get("fields", []):
            status, key = f.get("status"), f.get("key")
            try:
                value = json.loads(f.get("value_json") or "null")
            except json.JSONDecodeError as e:
                raise ValueError(f"field {key}: value_json is not JSON ({e})") from None
            ref = ctx.ref(key) if isinstance(key, str) else None
            if (ref is not None and ref.field_type in LIST_TYPES
                    and status == "value" and not isinstance(value, list)):
                value = [] if value is None else [value]  # one option named: still a list
            if status == "value":
                fields[key] = {"status": "value", "value": value,
                               "confidence": f.get("confidence", 0),
                               "source_text": f.get("source_text", "")}
            elif status == "ambiguous":
                fields[key] = {"status": "ambiguous", "candidates": value,
                               "source_text": f.get("source_text", "")}
            else:
                fields[key] = {"status": status}
        candidate = {
            "target": tk,
            "confidence": c.get("confidence"),
            "item": text_or_none(c.get("item")),
            "item_candidates": c.get("item_candidates", []),
            "item_text": text_or_none(c.get("item_text")),
            "fields": fields,
            "content": text_or_none(c.get("content")),
            "search_query": text_or_none(c.get("search_query")),
        }
        if ctx.web_research:
            candidate["web_query"] = text_or_none(c.get("web_query", ""))
            candidate["web_media"] = c.get("web_media", "text")
        if ctx.name_targets and tk in ctx.target_labels:
            # A thinking aid only; the key is what counts, so the label follows it.
            candidate = {"target_name": ctx.target_labels[tk], **candidate}
        candidates.append(candidate)
    return {"notes": flat.get("notes", ""), "clarify": text_or_none(flat.get("clarify", "")),
            "intent": flat.get("intent"), "candidates": candidates}


def check(answer: dict, schema: dict) -> str:
    """"" when the answer fits the full per-request schema, else what is wrong with it.

    A candidate is one branch of an anyOf, so a plain validation reports only "candidates/0 is
    not valid under any of the given schemas" — useless to a retry. Each candidate is checked
    against the branch its own target key selects instead, which names the actual field."""
    validator = jsonschema.Draft202012Validator
    candidates = answer.get("candidates")
    items = schema["properties"]["candidates"].get("items", {})
    branches = {b["properties"]["target"]["const"]: b for b in items.get("anyOf", [])}
    if isinstance(candidates, list) and branches:
        top = {**schema, "properties": {**schema["properties"],
                                        "candidates": {"type": "array"}}}
        errors = list(validator(top).iter_errors(answer))
        for i, c in enumerate(candidates):
            branch = branches.get(c.get("target")) if isinstance(c, dict) else None
            if branch is None:
                errors += validator(items).iter_errors(c)  # reports the unknown target
                continue
            errors += [(e, i) for e in validator(branch).iter_errors(c)]
    else:
        errors = list(validator(schema).iter_errors(answer))

    def where(err) -> str:
        e, i = err if isinstance(err, tuple) else (err, None)
        path = [*(["candidates", i] if i is not None else []), *e.absolute_path]
        return f"{'/'.join(map(str, path)) or 'answer'}: {e.message[:200]}"

    return "; ".join(where(e) for e in errors[:5])


class ClaudeClient:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout_s: float = 30.0,
        max_tokens: int = 4096,
        client: anthropic.AsyncAnthropic | None = None,
        health: Health | None = None,
    ) -> None:
        self.model = model
        self._max_tokens = max_tokens
        self._health = health or Health()
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
            raise LLMUnavailable(describe(e), self._health.record(e)) from None
        self._health.ok()
        return [self.model, info.id]

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        base = build_messages(text, context, cloud=True)
        system = base[0]["content"] + FLAT_FORMAT_NOTE
        messages: list[dict] = base[1:]
        output_config = {"format": {"type": "json_schema", "schema": flat_schema(context)}}
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
                raise LLMUnavailable(describe(e), self._health.record(e)) from None
            self._health.ok()
            raw = next((b.text for b in resp.content if b.type == "text"), "")
            if resp.stop_reason == "refusal":
                raise LLMInvalidOutput("claude declined to answer", raw=raw)
            if resp.stop_reason == "max_tokens":
                error = "output truncated (max_tokens)"
            else:
                try:
                    answer = to_interpretation(json.loads(raw), context)
                    error = check(answer, schema)
                    if not error:
                        interp = Interpretation.model_validate(answer)
                except (ValueError, ValidationError) as e:  # JSONDecodeError is a ValueError
                    error = str(e)[:800]
                if not error:
                    return interp, LLMTrace(
                        model=self.model,
                        messages=[{"role": "system", "content": system}, *messages],
                        raw_response=json.dumps(answer, ensure_ascii=False),
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
