"""Multi-step goals: Claude breaks the goal into one-action steps, then after every step says
whether the goal is reached or what to do next. The steps themselves run through the ordinary
pipeline (app.conversation.orchestrator), so a step is validated, can ask the user, and can be
undone like any single message."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import anthropic

from app.conversation.plan import MAX_STEPS, PlanState
from app.llm.prompts import PLAN_NEXT_PROMPT, PLAN_PROMPT, plan_message, progress_message

log = logging.getLogger(__name__)

_STRING = {"type": "string"}
PLAN_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["goal", "steps"],
    "properties": {"goal": _STRING, "steps": {"type": "array", "items": _STRING}},
}
NEXT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["done", "summary", "next_step"],
    "properties": {"done": {"type": "boolean"}, "summary": _STRING, "next_step": _STRING},
}


class PlanError(Exception):
    pass


@dataclass(frozen=True)
class Verdict:
    done: bool
    summary: str
    next_step: str


class Planner:
    def __init__(
        self, api_key: str, model: str, *, timeout_s: float = 60.0,
        client: anthropic.AsyncAnthropic | None = None,
    ) -> None:
        self.model = model
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def plan(self, request: str, workspace: str) -> tuple[str, list[str]]:
        """The goal and its steps. Raises PlanError when no usable plan came back."""
        data = await self._ask(PLAN_PROMPT, PLAN_SCHEMA, plan_message(request, workspace))
        goal = str(data.get("goal", "")).strip()
        steps = [s.strip() for s in data.get("steps", []) if isinstance(s, str) and s.strip()]
        if not goal or not steps:
            raise PlanError("empty plan")
        return goal, steps[:MAX_STEPS]

    async def next(self, state: PlanState, workspace: str) -> Verdict:
        """Done, or the next step. A failed check ends the plan rather than guessing on."""
        try:
            data = await self._ask(PLAN_NEXT_PROMPT, NEXT_SCHEMA,
                                   progress_message(state, workspace))
        except PlanError as e:
            log.warning("plan check failed (%s); stopping the plan", e)
            return Verdict(done=True, summary="", next_step="")
        next_step = str(data.get("next_step", "")).strip()
        done = bool(data.get("done")) or not next_step
        return Verdict(done=done, summary=str(data.get("summary", "")).strip(),
                       next_step=next_step)

    async def _ask(self, system: str, schema: dict, content: str) -> dict:
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=2048, system=system,
                messages=[{"role": "user", "content": content}],
                output_config={"format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.APIError as e:
            raise PlanError(f"claude {getattr(e, 'status_code', None) or type(e).__name__}") \
                from None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise PlanError("answer is not JSON") from None
        if not isinstance(data, dict):
            raise PlanError("answer is not an object")
        return data
