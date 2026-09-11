"""Pending-session state: what the bot remembers while a CLARIFY round-trip is in flight, across
a bot restart and across a workspace rediscovery that regenerates every context key.

The central rule: a session stores Notion ids and typed values, never context keys. Context keys
(t1, t1.f2, t1.f2.o3, t1.i4, ...) are positional and assigned fresh on every request by
ContextBuilder (app/llm/context.py); a persisted "t1.f2.o3" would silently point at a different
field after a rediscovery. See documentation/DATA_MODEL.md §4 ("Answer keys across a context
rebuild"). The one sanctioned exception is the persisted `Question` (validation/policy.py): its
target_key/field_key/options[].key ARE context keys, kept on purpose so Task 3's resolver can
re-resolve them against a freshly built Context after a rediscovery.

This module does not import app.conversation.reply, and reply.py does not import this module
(see reply.py's module docstring) — they are independent layers joined by the orchestrator
(Task 5)."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.audit.store import AuditStore
from app.commands.jsonvalue import to_json_value
from app.validation.policy import Decision, Question
from app.validation.semantic import ValidationResult, VCandidate, VField

MAX_QUESTIONS = 3


def _utc(v: datetime) -> datetime:
    if v.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return v.astimezone(UTC)


class PendingField(BaseModel):
    """One target field flattened from a VField: Notion field id and JSON-safe typed value(s),
    never the context key that named it in this request's Context."""

    model_config = ConfigDict(extra="forbid")
    field_id: str
    name: str
    type: str
    status: str
    value: Any = None
    candidates: list[Any] = Field(default_factory=list)
    confidence: float = 1.0
    source_text: str = ""


class PendingCandidate(BaseModel):
    """One VCandidate flattened to Notion ids: the target's id, the resolved item's page id (if
    any), and its fields. Not the candidate's context key (VCandidate.key)."""

    model_config = ConfigDict(extra="forbid")
    target_id: str
    confidence: float
    item_page_id: str | None
    item_text: str | None
    content: str | None
    search_query: str | None
    fields: list[PendingField]


class AnswerOption(BaseModel):
    """One button on the question currently on screen: what it means in Notion terms. `id` is a
    short opaque token chosen by the caller building the option list (Task 3's resolver.py) —
    o0..o7, or a literal like "confirm"/"other"/"add_new"/"cancel"/"inbox" — never a QOption.key,
    since QOption.key embeds Notion page ids and would blow Telegram's 64-byte callback budget.
    Its format is not validated here."""

    model_config = ConfigDict(extra="forbid")
    id: str
    label: str
    target_id: str | None = None
    item_page_id: str | None = None
    field_id: str | None = None
    option_id: str | None = None
    value: Any = None


class PendingSession(BaseModel):
    """Everything needed to resume a CLARIFY round-trip: the original request, every validated
    candidate (so an alternate target/item can be picked without re-running the LLM), the
    question currently on screen, and the answer table for its buttons."""

    model_config = ConfigDict(extra="forbid")
    chat_id: int
    event_id: int
    token: str
    original_text: str
    intent: str
    intent_confidence: float
    candidates: list[PendingCandidate]
    question: Question
    options: list[AnswerOption]
    asked: list[str] = Field(default_factory=list)
    created_at: datetime
    expires_at: datetime

    @field_validator("created_at", "expires_at")
    @classmethod
    def _normalize_utc(cls, v: datetime) -> datetime:
        return _utc(v)

    @property
    def best(self) -> PendingCandidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def is_exhausted(self) -> bool:
        return len(self.asked) >= MAX_QUESTIONS


def _pending_field(f: VField) -> PendingField:
    return PendingField(
        field_id=f.field.id, name=f.field.name, type=f.field.type, status=f.status,
        value=to_json_value(f.value), candidates=[to_json_value(c) for c in f.candidates],
        confidence=f.confidence, source_text=f.source_text,
    )


def _pending_candidate(c: VCandidate) -> PendingCandidate:
    return PendingCandidate(
        target_id=c.target.id, confidence=c.confidence,
        item_page_id=c.item.id if c.item is not None else None, item_text=c.item_text,
        content=c.content, search_query=c.search_query,
        fields=[_pending_field(f) for f in c.fields.values()],
    )


def session_from_decision(
    chat_id: int, event_id: int, text: str, result: ValidationResult, decision: Decision,
    options: list[AnswerOption], *, now: datetime, ttl_s: int, asked: list[str],
) -> PendingSession:
    """Flatten a Decision that carries a candidate and at least one Question into a
    PendingSession — a CLARIFY, or the item_not_found/nothing_to_write REJECT cases (both carry
    the candidate and a single Question on purpose: DATA_MODEL.md §4, so Plan 3 can act on them
    without re-running the LLM, e.g. an "add new" answer for item_not_found). An EXECUTE decision
    has nothing to ask and still raises. `options` is the answer table for decision.questions[0]
    (Task 3's resolver.options_for(...)); `asked` is carried forward by the orchestrator across
    round-trips, not computed here. Generates a fresh `token`."""
    if decision.candidate is None or not decision.questions:
        raise ValueError(
            "session_from_decision requires a decision with a candidate and a question "
            "(CLARIFY, or the item_not_found/nothing_to_write REJECT cases)"
        )
    return PendingSession(
        chat_id=chat_id, event_id=event_id, token=secrets.token_hex(4), original_text=text,
        intent=result.intent, intent_confidence=result.intent_confidence,
        candidates=[_pending_candidate(c) for c in result.candidates],
        question=decision.questions[0], options=list(options), asked=list(asked),
        created_at=now, expires_at=now + timedelta(seconds=ttl_s),
    )


class SessionStore:
    """Thin JSON-payload layer over AuditStore's `sessions` table."""

    def __init__(self, audit: AuditStore) -> None:
        self._audit = audit

    def save(self, s: PendingSession) -> None:
        self._audit.save_session(s.chat_id, s.model_dump_json(), s.expires_at)

    def get(self, chat_id: int, now: datetime) -> PendingSession | None:
        payload = self._audit.get_session(chat_id, now)
        if payload is None:
            return None
        try:
            return PendingSession.model_validate_json(payload)
        except ValidationError:
            self._audit.delete_session(chat_id)
            return None

    def drop(self, chat_id: int) -> None:
        self._audit.delete_session(chat_id)

    def pop_expired_one(self, chat_id: int, now: datetime) -> PendingSession | None:
        """Delete and return one chat's session only if it has already expired. The orchestrator
        asks this before `get`, which drops an expired row silently: the question is gone either
        way, but its original text can still be rescued into the inbox instead of being lost."""
        payload = self._audit.pop_expired_session(chat_id, now)
        if payload is None:
            return None
        try:
            return PendingSession.model_validate_json(payload)
        except ValidationError:
            return None

    def pop_expired(self, now: datetime) -> list[PendingSession]:
        """Return and delete every session whose expires_at <= now (the inbox sweeper).

        expired_sessions(now) only discovers candidates; the actual delete goes through
        pop_expired_session(chat_id, now), which re-checks expiry under the same lock
        acquisition as the delete. That closes the TOCTOU window between the scan and the
        delete: if a session is renewed (save_session with a later expires_at) after the scan
        but before its turn to be popped, pop_expired_session finds it no longer expired,
        deletes nothing, and this method silently skips it instead of destroying live state."""
        out: list[PendingSession] = []
        for chat_id, _ in self._audit.expired_sessions(now):
            session = self.pop_expired_one(chat_id, now)
            if session is not None:
                out.append(session)
        return out
