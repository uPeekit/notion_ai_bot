"""Changing one place on a page instead of rewriting the whole of it.

The page is read and shown to the model as numbered lines (`app/notion/to_markdown.py:
outline`, `numbered`); the model answers with a short list of operations against those numbers,
or — when the instruction really is "rewrite all of this" — with the whole new text in `full`.
Both come back from one call, and the model picks.

Why an edit script rather than a rewritten page:

* it keeps block ids, so comments, links and anything pointing at a block survive;
* it costs a couple of hundred output tokens instead of the whole page again;
* a picture is just a line, so removing one needs no special case;
* the reply can say exactly what changed, not only that the page got shorter.

Nothing here reads or writes anything. Line numbers are the model's only way to name a block,
and `check` is what turns them back into blocks the caller actually read — a model can never
name a block that was not on the page in front of it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import anthropic

from app.llm.health import Health, describe
from app.llm.prompts import EDIT_PROMPT, edit_message
from app.llm.rewrite import unfence
from app.notion.to_markdown import Line

log = logging.getLogger(__name__)

OPS = ("replace", "insert", "delete", "image")
MAX_TOKENS = 8_000
# What one page may show the model. Past this the caller narrows to a section first.
MAX_INPUT = 20_000
# One message may change this many places. More than that is not an edit.
MAX_OPS = 40
# A script that throws away more than this share of a page is not an edit either — it is a
# rewrite the model should have answered with `full`, and acting on it could empty a page on a
# misread instruction.
MAX_DELETE_SHARE = 0.5
_STRING = {"type": "string"}
_INT = {"type": "integer"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


EDIT_SCHEMA = _obj({
    "op": {"enum": list(OPS)},
    # replace/delete: which line. insert/image: the line to put it after.
    "at": _INT,
    # delete only: the last line of a range, or 0 for the single line named by `at`.
    "to": _INT,
    "text": _STRING,
    "url": _STRING,
    "caption": _STRING,
})
PLAN_SCHEMA = _obj({"edits": {"type": "array", "items": EDIT_SCHEMA}, "full": _STRING})


class EditError(Exception):
    """`reason` is a code from app/llm/health.py when Claude could not be used at all."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


class NothingToChange(EditError):
    """The model read the page and found nothing the instruction applies to. Not a failure:
    the user asked for something that is not there, and telling them so is the answer."""


@dataclass(frozen=True)
class Edit:
    op: str
    at: int = 0
    to: int = 0
    text: str = ""
    url: str = ""
    caption: str = ""

    @property
    def span(self) -> range:
        """The lines a delete covers, `at` alone when no range was given."""
        end = self.to if self.to >= self.at else self.at
        return range(self.at, end + 1)


@dataclass
class EditPlan:
    edits: list[Edit] = field(default_factory=list)
    # The model chose to rewrite everything rather than edit places. The caller then uses the
    # whole-text path, which has its own rules about what may be replaced.
    full: str = ""

    def __bool__(self) -> bool:
        return bool(self.edits or self.full.strip())


def parse(data: dict) -> EditPlan:
    """The model's raw answer as a plan. Nothing here judges whether the edits are allowed."""
    edits = []
    for raw in data.get("edits") or []:
        if not isinstance(raw, dict):
            continue
        try:
            edits.append(Edit(
                op=str(raw.get("op", "")),
                at=int(raw.get("at") or 0),
                to=int(raw.get("to") or 0),
                text=str(raw.get("text", "")),
                url=str(raw.get("url", "")).strip(),
                caption=str(raw.get("caption", "")).strip(),
            ))
        except (TypeError, ValueError):
            continue
    return EditPlan(edits=edits, full=unfence(str(data.get("full", ""))))


def _anchor(at: int, by_n: dict, last: int) -> int:
    """Which line a new one goes after.

    Notion can only insert *after* an existing block, so "put it at the top" comes back as
    line 0 and has to become line 1 — the top is far closer to what was asked than the
    bottom, which is where an out-of-range number would otherwise land."""
    if at in by_n:
        return at
    return 1 if at < 1 and last else last


def check(plan: EditPlan, lines: list[Line]) -> tuple[EditPlan, list[str]]:
    """The edits that may actually be carried out, and what was refused and why (for the log).

    This is the gate the whole feature rests on. Every rule here is about what the *code* read,
    never about what the instruction said: a line the model invented, a sub-page it tried to
    delete, or a script that would empty the page cannot get through, however the request was
    phrased."""
    if plan.full.strip():
        return EditPlan(full=plan.full), []
    by_n = {line.n: line for line in lines}
    last = max(by_n, default=0)
    out: list[Edit] = []
    refused: list[str] = []
    for edit in plan.edits[:MAX_OPS]:
        if edit.op not in OPS:
            refused.append(f"unknown op {edit.op!r}")
            continue
        if edit.op == "replace":
            line = by_n.get(edit.at)
            if line is None:
                refused.append(f"replace {edit.at}: no such line")
            elif not line.editable:
                refused.append(f"replace {edit.at}: {line.kind} is not editable text")
            elif not edit.text.strip():
                refused.append(f"replace {edit.at}: nothing to put there")
            else:
                out.append(edit)
        elif edit.op == "delete":
            keep = [n for n in edit.span if n in by_n and by_n[n].removable]
            blocked = [n for n in edit.span if n in by_n and not by_n[n].removable]
            if blocked:
                refused.append(f"delete {edit.at}-{edit.to}: "
                               f"{', '.join(by_n[n].kind for n in blocked)} stays")
            for n in keep:  # one op per line, so a range cannot half-apply
                out.append(Edit(op="delete", at=n))
            if not keep:
                refused.append(f"delete {edit.at}: nothing there may be removed")
        elif edit.op == "insert":
            if not edit.text.strip():
                refused.append("insert: nothing to insert")
                continue
            out.append(Edit(op="insert", at=_anchor(edit.at, by_n, last),
                            text=edit.text))
        else:  # image
            if not edit.url.lower().startswith(("http://", "https://")):
                refused.append(f"image: {edit.url[:60]!r} is not a web address")
                continue
            out.append(Edit(op="image", at=_anchor(edit.at, by_n, last),
                            url=edit.url, caption=edit.caption))
    deletes = sum(1 for e in out if e.op == "delete")
    if lines and deletes > max(1, int(len(lines) * MAX_DELETE_SHARE)):
        # More than half the page. If that is really what was asked for, the model was supposed
        # to answer with `full`, which goes through the rewrite path and its own safeguards.
        refused.append(f"{deletes} of {len(lines)} lines deleted: that is a rewrite, not an edit")
        return EditPlan(), refused
    return EditPlan(edits=out), refused


class Editor:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 120.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def plan(self, page: str, instruction: str) -> tuple[EditPlan, int, int]:
        """What to change, and what it cost. Raises EditError."""
        if not page.strip():
            raise EditError("nothing to edit")
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=EDIT_PROMPT,
                messages=[{"role": "user",
                           "content": edit_message(page[:MAX_INPUT], instruction)}],
                output_config={"format": {"type": "json_schema", "schema": PLAN_SCHEMA}},
            )
        except anthropic.APIError as e:
            raise EditError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "refusal":
            raise EditError("claude declined")
        if resp.stop_reason == "max_tokens":
            raise EditError("answer cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            plan = parse(json.loads(text or "{}"))
        except json.JSONDecodeError:
            raise EditError("not JSON") from None
        if not plan:
            raise NothingToChange("no changes")
        log.info("%s planned %d edits%s", self.model, len(plan.edits),
                 " (whole text)" if plan.full else "")
        return plan, resp.usage.input_tokens, resp.usage.output_tokens
