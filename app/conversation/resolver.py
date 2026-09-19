"""Task 3: turns a button press back into an updated candidate/session, and a PendingSession
back into a fresh ValidationResult — without a second LLM call, and correctly even when the
workspace was rediscovered and every context key changed underneath it.

`apply_answer` is pure and id-based: it consumes only `PendingSession` and an opaque
`option_id`, never a `Context` or `WorkspaceSnapshot`. `options_for` is the mirror image and the
only place that translates `Question.options` (context keys, positional and rediscovery-
sensitive) into `AnswerOption`s (Notion ids, stable across a rediscovery) — it must run while the
Context that produced the Question is still in hand. `rebuild_candidate` / `result_from_session`
turn a persisted session back into the same shapes `SemanticValidator`/`Policy` already speak,
tolerating a changed workspace: a renamed target/field/option still resolves by id (via
Context.target_key/field_key/option_key/item_key, the reverse of ContextBuilder's positional
assignment); anything actually gone is dropped rather than raising.

Does not import app.conversation.reply or python-telegram-bot (see reply.py's own module
docstring): this module produces ids and typed values, not button rows or display text."""

from __future__ import annotations

from collections.abc import Callable

from app import texts
from app.commands.jsonvalue import to_json_value
from app.conversation.session import AnswerOption, PendingCandidate, PendingField, PendingSession
from app.llm.context import Context, KeyRef
from app.notion.snapshot import Field, Option, Target, WorkspaceSnapshot
from app.validation.policy import Decision, Question
from app.validation.semantic import Issue, ValidationResult, VCandidate, VField, typed_value

_OPTION_TYPES = frozenset({"select", "status"})
_LIST_OPTION_TYPES = frozenset({"multi_select", "relation"})


class _Unresolvable(Exception):
    """A stored option id no longer exists on the field in the fresh snapshot."""


# --------------------------------------------------------------------------------------------
# rebuild_candidate / result_from_session
# --------------------------------------------------------------------------------------------

def _field_def(target: Target, ref: KeyRef) -> Field:
    """Same fallback SemanticValidator._field_def uses for a synthetic page title field, which
    is not a real entry on Target.fields."""
    fdef = target.field(ref.field_id or "")
    if fdef is None:
        fdef = Field(id=ref.field_id or "", name=ref.name, type="title", required=True,
                      options=[], relation_data_source_id=None, description="")
    return fdef


def _rebuild_option(ctx: Context, target_id: str, field_id: str, item: dict) -> Option:
    key = ctx.option_key(target_id, field_id, item["id"])
    if key is None:
        raise _Unresolvable
    ref = ctx.ref(key)
    assert ref is not None
    return Option(item["id"], ref.name)


def _rebuild_value(ctx: Context, target_id: str, field_id: str, fk: str, ftype: str, raw):
    if raw is None:
        return None
    if ftype in _OPTION_TYPES:
        return _rebuild_option(ctx, target_id, field_id, raw)
    if ftype in _LIST_OPTION_TYPES:
        return [_rebuild_option(ctx, target_id, field_id, x) for x in raw]
    if ftype == "date":
        return typed_value(ctx, fk, "date", raw)
    return raw


def _rebuild_field(
    pf: PendingField, target: Target, ctx: Context, target_id: str, issues: list[Issue]
) -> VField | None:
    fk = ctx.field_key(target_id, pf.field_id)
    if fk is None:
        return None  # field no longer surfaced in the fresh context: drop, no issue
    ref = ctx.ref(fk)
    assert ref is not None
    fdef = _field_def(target, ref)
    ftype = ref.field_type or fdef.type
    try:
        value = (_rebuild_value(ctx, target_id, pf.field_id, fk, ftype, pf.value)
                 if pf.status == "value" else None)
        candidates = ([_rebuild_value(ctx, target_id, pf.field_id, fk, ftype, c)
                       for c in pf.candidates] if pf.status == "ambiguous" else [])
    except _Unresolvable:
        issues.append(Issue("SEM_UNKNOWN_KEY",
                             f"{pf.name}: a selected option no longer exists", fk))
        return None
    return VField(key=fk, ref=ref, field=fdef, status=pf.status, value=value,
                  candidates=candidates, confidence=pf.confidence, source_text=pf.source_text)


def rebuild_candidate(
    pc: PendingCandidate, snapshot: WorkspaceSnapshot, ctx: Context, issues: list[Issue]
) -> VCandidate | None:
    """Resolve one PendingCandidate back into a VCandidate against `snapshot`/`ctx`. Never
    raises: a gone target returns None; a gone field is dropped; a field whose stored option no
    longer exists is dropped with a SEM_UNKNOWN_KEY issue; a gone item is dropped — the resolved
    one leaving item=None, a remembered alternative leaving item_candidates one shorter.
    `issues` is mutated in place, matching SemanticValidator._candidate's own convention."""
    target = snapshot.target(pc.target_id)
    if target is None:
        return None
    key = ctx.target_key(pc.target_id)
    if key is None:
        return None
    fields: dict[str, VField] = {}
    for pf in pc.fields:
        vf = _rebuild_field(pf, target, ctx, pc.target_id, issues)
        if vf is not None:
            fields[vf.key] = vf
    item = target.item(pc.item_page_id) if pc.item_page_id is not None else None
    # Restored, not dropped: Policy asks the `item` question only while these are non-empty, so
    # an emptied list turns the second question of a two-question round-trip into an
    # item_not_found REJECT that offers to create a duplicate of a page that does exist.
    item_candidates = [i for i in (target.item(pid) for pid in pc.item_candidates)
                       if i is not None]
    return VCandidate(
        key=key, target=target, confidence=pc.confidence, item=item,
        item_candidates=item_candidates, item_text=pc.item_text, fields=fields,
        content=pc.content, search_query=pc.search_query, web_query=pc.web_query,
    )


def result_from_session(
    s: PendingSession, snapshot: WorkspaceSnapshot, ctx: Context
) -> ValidationResult:
    """Rebuild every candidate the session remembers, dropping the ones that no longer resolve,
    and hand back a ValidationResult ready for Policy.evaluate() — exactly the shape the first
    LLM round-trip would have produced, but without calling the LLM again."""
    issues: list[Issue] = []
    candidates: list[VCandidate] = []
    for pc in s.candidates:
        vc = rebuild_candidate(pc, snapshot, ctx, issues)
        if vc is not None:
            candidates.append(vc)
    candidates.sort(key=lambda c: c.confidence, reverse=True)
    return ValidationResult(s.intent, s.intent_confidence, candidates, issues)


# --------------------------------------------------------------------------------------------
# next_question
# --------------------------------------------------------------------------------------------

def _answer_key(qtype: str, field_id: str | None) -> str:
    return f"{qtype}:{field_id or ''}"


def next_question(decision: Decision, asked: list[str], ctx: Context) -> Question | None:
    """First question in `decision.questions` whose answer key is not already in `asked`, or
    None once every question has been answered. The answer key mirrors apply_answer's own
    (q.type + ":" + Notion field id), computed here through the *current* Context since
    next_question, unlike apply_answer, is allowed to use one."""
    for q in decision.questions:
        field_id = ctx.ref(q.field_key).field_id if q.field_key else None
        if _answer_key(q.type, field_id) not in asked:
            return q
    return None


# --------------------------------------------------------------------------------------------
# options_for
# --------------------------------------------------------------------------------------------

_CANCEL = AnswerOption(id="cancel", label=texts.BTN_CANCEL)
_INBOX = AnswerOption(id="inbox", label=texts.BTN_INBOX)


def options_for(
    q: Question, candidate: VCandidate, result: ValidationResult, ctx: Context
) -> list[AnswerOption]:
    """Translate one Question's context-keyed options (plus its type-specific extra buttons)
    into id-based AnswerOptions. The only place a context key is read; must run while `ctx` (the
    Context that produced `q`) is still in hand."""
    out: list[AnswerOption] = []
    if q.type == "target":
        for i, o in enumerate(q.options):
            ref = ctx.ref(o.key)
            assert ref is not None
            out.append(AnswerOption(id=f"o{i}", label=o.label, target_id=ref.target_id))
    elif q.type == "item":
        for i, o in enumerate(q.options):
            page_id = o.key.split(".item:", 1)[1]
            out.append(AnswerOption(id=f"o{i}", label=o.label, item_page_id=page_id))
    elif q.type == "item_not_found":
        title = candidate.target.title_field()
        out.append(AnswerOption(id="add_new", label=texts.BTN_ADD_NEW,
                                 field_id=title.id if title else None))
    elif q.type == "field_required":
        for i, o in enumerate(q.options):
            ref = ctx.ref(o.key)
            assert ref is not None
            out.append(AnswerOption(id=f"o{i}", label=o.label, field_id=ref.field_id,
                                     option_id=ref.option_id))
    elif q.type == "field_ambiguous":
        assert q.field_key is not None
        field = candidate.fields[q.field_key]
        fref = ctx.ref(q.field_key)
        assert fref is not None
        for i, o in enumerate(q.options):
            idx = int(o.key.split("#", 1)[1])
            out.append(AnswerOption(id=f"o{i}", label=o.label, field_id=fref.field_id,
                                     value=to_json_value(field.candidates[idx])))
    elif q.type == "intent_confirm":
        out.append(AnswerOption(id="confirm", label=texts.BTN_CONFIRM))
        out.append(AnswerOption(id="other", label=texts.BTN_OTHER))
    if q.type == "target":
        out.append(AnswerOption(id="other", label=texts.BTN_OTHER))
    elif q.type in ("date", "field_confirm"):
        fref = ctx.ref(q.field_key) if q.field_key else None
        field_id = fref.field_id if fref else None
        out.append(AnswerOption(id="confirm", label=texts.BTN_CONFIRM, field_id=field_id))
        out.append(AnswerOption(id="other", label=texts.BTN_OTHER, field_id=field_id))
    out.append(_CANCEL)
    out.append(_INBOX)
    return out


# --------------------------------------------------------------------------------------------
# apply_answer
# --------------------------------------------------------------------------------------------

def _update_best(
    candidates: list[PendingCandidate], fn: Callable[[PendingCandidate], PendingCandidate]
) -> list[PendingCandidate]:
    if not candidates:
        return candidates
    return [fn(candidates[0]), *candidates[1:]]


def _update_field(c: PendingCandidate, field_id: str | None, **updates) -> PendingCandidate:
    fields = [f.model_copy(update=updates) if f.field_id == field_id else f for f in c.fields]
    return c.model_copy(update={"fields": fields})


def apply_answer(s: PendingSession, option_id: str) -> tuple[PendingSession, str | None]:
    """Pure, id-based: no Context, no WorkspaceSnapshot, no I/O. Returns the updated session and
    a control verb (None = re-evaluate with Policy, "cancel", "inbox", "free_text")."""
    if option_id == "cancel":
        return s, "cancel"
    if option_id == "inbox":
        return s, "inbox"
    opt = next((o for o in s.options if o.id == option_id), None)
    if opt is None:
        return s, None  # stale/unknown button: no-op

    q = s.question
    if option_id == "other" and q.type in ("date", "field_confirm", "target", "intent_confirm"):
        return s, "free_text"

    # The asked-key's field component only exists when the question itself is field-keyed
    # (field_required/field_ambiguous/field_confirm/date); this mirrors next_question's own
    # `ctx.ref(q.field_key).field_id if q.field_key else None` without needing a Context here,
    # since q.field_key's mere presence (not its value) is all that's being tested.
    field_id = opt.field_id if q.field_key is not None else None
    asked = [*s.asked, _answer_key(q.type, field_id)]
    candidates = list(s.candidates)
    intent = s.intent
    intent_confidence = s.intent_confidence

    if q.type == "target":
        chosen = next((c for c in candidates if c.target_id == opt.target_id), None)
        if chosen is not None:
            candidates = [chosen.model_copy(update={"confidence": 1.0})]
    elif q.type == "intent_confirm":
        intent_confidence = 1.0
    elif q.type == "item":
        candidates = _update_best(
            candidates, lambda c: c.model_copy(update={"item_page_id": opt.item_page_id}))
    elif q.type == "item_not_found":
        intent = "create"

        def _add_new(c: PendingCandidate) -> PendingCandidate:
            updated = _update_field(c, opt.field_id, status="value", value=c.item_text or "",
                                     confidence=1.0)
            return updated.model_copy(update={"item_page_id": None})

        candidates = _update_best(candidates, _add_new)
    elif q.type == "field_required":
        value = {"id": opt.option_id, "name": opt.label}
        candidates = _update_best(candidates, lambda c: _update_field(
            c, opt.field_id, status="value", value=value, confidence=1.0))
    elif q.type == "field_ambiguous":
        candidates = _update_best(candidates, lambda c: _update_field(
            c, opt.field_id, status="value", value=opt.value, confidence=1.0, candidates=[]))
    elif q.type in ("date", "field_confirm"):
        candidates = _update_best(
            candidates, lambda c: _update_field(c, opt.field_id, confidence=1.0))

    updated = s.model_copy(update={
        "candidates": candidates, "intent": intent, "intent_confidence": intent_confidence,
        "asked": asked,
    })
    return updated, None
