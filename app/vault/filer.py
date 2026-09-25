"""The Obsidian side's interpreter: one light model call that turns a message into vault
actions, and a deterministic check of what came back.

It knows nothing about Notion. Its context is the user's own guide note, the folders and tags
the vault already uses, and the notes whose names share a word with the message — about a tenth
of what the Notion interpreter is shown. It never asks a question: anything that does not check
out becomes a line in the inbox note, which is always a safe place to put words."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime

import anthropic
from pydantic import ValidationError

from app import texts
from app.llm.health import Health, describe
from app.llm.prompts import FILER_PROMPT, filer_message, filer_vault
from app.vault.index import VaultIndex
from app.vault.writer import VaultAction

log = logging.getLogger(__name__)

ACTIONS = ("task", "note", "append", "update", "rewrite", "log", "search", "agenda",
           "inbox")
MAX_ACTIONS = 10
MAX_TOKENS = 4000
MAX_BODY_LINES = 200
MAX_TEXT = 4000
MAX_CANDIDATES = 40
MAX_GUIDE = 4000
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_STRING = {"type": "string"}


def _obj(props: dict) -> dict:
    return {"type": "object", "additionalProperties": False, "required": list(props),
            "properties": props}


ACTION_SCHEMA = _obj({
    "action": {"enum": list(ACTIONS)},
    "text": _STRING,
    "note": _STRING,
    "folder": _STRING,
    "title": _STRING,
    "heading": _STRING,
    "body": {"type": "array", "items": _STRING},
    "props": {"type": "array", "items": _obj({"name": _STRING, "value": _STRING})},
    "tags": {"type": "array", "items": _STRING},
    "due": _STRING,
    "repeat": _STRING,
    "countdown": {"type": "boolean"},
    "done": {"type": "boolean"},
    "task": _STRING,
    "due_from": _STRING,
    "due_to": _STRING,
    "scope": {"enum": ["day", "now", "any"]},
})
FILER_SCHEMA = _obj({"actions": {"type": "array", "items": ACTION_SCHEMA}})


class FilerError(Exception):
    """`reason` is a code from app/llm/health.py when the call failed because Claude
    could not be used at all, and "" for every other failure."""

    def __init__(self, message: str, reason: str = "") -> None:
        super().__init__(message)
        self.reason = reason


@dataclass(frozen=True)
class VaultContext:
    """What the filer is told about the vault. Names only — no paths, no ids."""

    guide: str
    folders: list[str]
    tags: list[str]
    known_notes: list[str]
    open_tasks: list[str]
    tasks_note: str
    daily_folder: str
    today: str
    weekday: str

    def json(self) -> str:
        return filer_vault(guide=self.guide, today=self.today, weekday=self.weekday,
                           folders=self.folders, tags=self.tags, tasks_note=self.tasks_note,
                           daily_folder=self.daily_folder, known_notes=self.known_notes,
                           open_tasks=self.open_tasks)


def context(index: VaultIndex, message: str, now: datetime) -> VaultContext:
    from app.llm.context import RU_WEEKDAYS  # weekday names live with the other prompts

    return VaultContext(
        guide=index.guide()[:MAX_GUIDE],
        folders=index.folders(),
        tags=index.tags(),
        known_notes=[n.name for n in index.candidates(message, MAX_CANDIDATES)],
        open_tasks=index.open_tasks(message),
        tasks_note=texts.VAULT_TASKS_NOTE,
        daily_folder=texts.VAULT_DAILY_DIR,
        today=now.strftime("%Y-%m-%d"),
        weekday=RU_WEEKDAYS[now.weekday()],
    )


def _props(raw: object) -> dict[str, str]:
    out: dict[str, str] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("name", "")).strip():
                out[str(item["name"]).strip()] = str(item.get("value", ""))
    elif isinstance(raw, dict):
        out = {str(k): str(v) for k, v in raw.items()}
    return out


def check(raw_actions: list[dict], index: VaultIndex, message: str) -> list[VaultAction]:
    """Every action the writer may safely carry out. An action naming a note or a folder that
    is not there is not guessed at — it becomes an inbox line holding the user's own words."""
    out: list[VaultAction] = []
    folders = set(index.folders())
    for raw in raw_actions[:MAX_ACTIONS]:
        if not isinstance(raw, dict):
            continue
        try:
            action = VaultAction(
                action=str(raw.get("action", "")).strip().lower(),
                text=str(raw.get("text", ""))[:MAX_TEXT],
                note=str(raw.get("note", "")).strip(),
                folder=str(raw.get("folder", "")).strip().strip("/"),
                title=str(raw.get("title", "")).strip(),
                heading=str(raw.get("heading", "")).strip(),
                body=[str(x) for x in (raw.get("body") or [])][:MAX_BODY_LINES],
                props=_props(raw.get("props")),
                tags=[str(t).strip().lstrip("#") for t in (raw.get("tags") or [])
                      if str(t).strip()],
                due=str(raw.get("due", "")).strip(),
                repeat=str(raw.get("repeat", "")).strip(),
                countdown=bool(raw.get("countdown")),
                done=raw.get("done") if isinstance(raw.get("done"), bool) else None,
                task=str(raw.get("task", "")).strip(),
                due_from=str(raw.get("due_from", "")).strip(),
                due_to=str(raw.get("due_to", "")).strip(),
                scope=str(raw.get("scope", "")).strip().lower(),
            )
        except ValidationError:
            continue
        if action.action not in ACTIONS:
            action = VaultAction(action="inbox", text=action.text or message)
        for field in ("due", "due_from", "due_to"):
            if not _DATE.match(getattr(action, field)):
                setattr(action, field, "")
        if action.scope not in ("day", "now", "any"):
            action.scope = ""
        if action.repeat and not action.repeat.lower().startswith("every"):
            action.repeat = ""
        if (action.action in ("append", "update", "rewrite")
                and index.by_name(action.note) is None):
            action = VaultAction(action="inbox",
                                 text=action.text or " ".join(action.body) or message)
        if action.action == "note":
            if action.folder not in folders:
                action.folder = texts.VAULT_NOTES_DIR
            if not (action.title or action.text).strip():
                continue
        if action.action in ("task", "log", "inbox") and not action.text.strip():
            continue
        # A rewrite with no instruction is a note emptied for no stated reason.
        if action.action == "rewrite" and not action.text.strip():
            continue
        if action.action == "agenda" and not (action.scope or action.due_from or action.due_to):
            action.scope = "now"
        if action.action == "search" and not (action.text.strip() or action.tags
                                              or action.folder or action.props
                                              or action.due_from or action.due_to):
            action = VaultAction(action="inbox", text=message)
        out.append(action)
    return out


class Filer:
    def __init__(self, api_key: str, model: str, *, timeout_s: float = 30.0,
                 client: anthropic.AsyncAnthropic | None = None,
                 health: Health | None = None) -> None:
        self.model = model
        self._health = health or Health()
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, timeout=timeout_s, max_retries=1)

    async def aclose(self) -> None:
        await self._client.close()

    async def file(self, message: str, ctx: VaultContext) -> tuple[list[dict], int, int]:
        """The model's raw actions, plus the tokens it cost. Raises FilerError."""
        try:
            resp = await self._client.messages.create(
                model=self.model, max_tokens=MAX_TOKENS, system=FILER_PROMPT,
                messages=[{"role": "user", "content": filer_message(message, ctx.json())}],
                output_config={"format": {"type": "json_schema", "schema": FILER_SCHEMA}},
            )
        except anthropic.APIError as e:
            raise FilerError(describe(e), self._health.record(e)) from None
        self._health.ok()
        if resp.stop_reason == "max_tokens":
            raise FilerError("answer cut off (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise FilerError("not JSON") from None
        actions = data.get("actions")
        if not isinstance(actions, list):
            raise FilerError("no actions")
        usage = getattr(resp, "usage", None)
        return (actions, getattr(usage, "input_tokens", 0) or 0,
                getattr(usage, "output_tokens", 0) or 0)
