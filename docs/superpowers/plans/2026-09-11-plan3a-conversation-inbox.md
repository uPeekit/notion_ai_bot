# Plan 3a: Conversation Core, Sessions, Inbox Fallback — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop between a `Decision` (Plan 2b) and a user-facing reply: ask one question at a time with buttons, persist the pending state across restarts, apply answers without a second LLM call where possible, execute, offer Undo — and never lose a message: anything the pipeline cannot resolve is appended to a user-designated **inbox** target in Notion.

**Architecture:** `Orchestrator` is the single entry point for every incoming message (`handle_text`, `handle_callback`, `undo`, `cancel`). It owns the pipeline already built in Plans 1–2b (Discovery → ContextBuilder → LLMClient → SemanticValidator → Policy → CommandBuilder → Executor) plus three new pieces: `session.py` (JSON-serialisable pending state persisted in the `sessions` table, keyed by Notion ids, not context keys), `resolver.py` (button answer → updated candidate → re-evaluated `Decision`, no LLM call), and `inbox.py` (a fallback command that appends the raw text to the designated target). Transport stays out: the orchestrator returns a `Reply(text, buttons, undo_id)`; Plan 3b renders it into Telegram.

**Tech Stack:** Python 3.12, pydantic 2, existing `AuditStore`, `Discovery`, `Executor`, `Policy`, `FakeNotionProvider` (`tests/fakes.py`), `tools/sample_workspace.py`.

**Spec:** `documentation/ARCHITECTURE.md` §4, §7, §8, §11; `documentation/DATA_MODEL.md` §4 (answer-key rebuild table), §6, §7; `documentation/FLOWS.md` F1–F14; `documentation/ERRORS.md`; IMPLEMENTATION_PLAN T-012, T-040, T-041, T-051.

**User decision driving the inbox (2026-09-11):** *"adding one special page to notion, something like miscellaneous/notes — so if bot couldn't decide it could just add it there so it would be still persisted, but I would check it later myself and move."* The inbox is a safety net for **lost** messages, not a replacement for clarification: the bot still asks when a question can resolve the request, but every question also carries a «В разное» button, and every REJECT / unusable-LLM-output / expired-session path writes the text to the inbox instead of dropping it.

## Global Constraints

- One `events` row per handled message, opened at entry and updated exactly once at exit (including error exits). `event_id` flows into every log record and into `executions.event_id`.
- Sessions persist **Notion ids and typed values**, never context keys (`t1.f2.o3`): keys are regenerated per request and do not survive rediscovery — see DATA_MODEL §4 "Answer keys across a context rebuild". A session is rebuilt against a fresh snapshot on every answer; anything that disappeared from the workspace is dropped, and if the target itself is gone the session is discarded with `SESSION_EXPIRED`.
- Callback payloads are opaque and short (≤ 48 ASCII bytes, Telegram's limit is 64): `a:<token>:<opt>` for answers, `u:<execution_id>` for undo. `<token>` is 8 hex chars regenerated for every question, so a button from a superseded question is rejected, not silently misapplied.
- At most `MAX_QUESTIONS = 3` clarification round-trips per session; the fourth unresolved question falls back to the inbox (or REJECT when the inbox is off). No question type is ever asked twice for the same field in one session.
- The orchestrator never raises to its caller: every exception path maps to a `Reply` with a user-facing Russian message from `texts.py` and an audited `error` column.
- The inbox write is a direct `AppendBlocks`/`CreateItem` command built by `inbox.py` — it does not go through the LLM, the validator or the policy, and a failed inbox write degrades to a plain error reply (never a retry loop, never a second fallback).
- All user-facing strings live in `app/texts.py`. No Russian string literals anywhere else in `app/` (a test greps for Cyrillic outside `texts.py`).
- Secrets never reach a reply, a log line, or the `sessions`/`events` payloads.
- `uv run ruff check .` and `uv run pytest -q` pass before each commit; trailer `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`; named `git add` only. PowerShell; uv may be at `$env:USERPROFILE\.local\bin\uv.exe`.

## File structure

```text
app/texts.py                      all Russian user-facing strings + formatters
app/conversation/__init__.py
app/conversation/reply.py         Button, Reply, format_execution/format_search/format_question
app/conversation/session.py       PendingField, PendingCandidate, AnswerOption, PendingSession, SessionStore
app/conversation/resolver.py      rebuild_candidate(), apply_answer(), result_from_session()
app/conversation/inbox.py         inbox_target(), inbox_command(), InboxMode
app/conversation/orchestrator.py  Orchestrator
app/audit/store.py                + expired_sessions(), pop_session()
app/config.py                     + inbox_mode, inbox_target_id
app/notion/descriptions.py        + TargetMeta.inbox
app/notion/snapshot.py            + Target.is_inbox
app/notion/discovery.py           _apply_meta sets is_inbox (+ INBOX_TARGET_ID override)
tests/test_texts.py, tests/test_reply.py, tests/test_session.py, tests/test_resolver.py,
tests/test_inbox.py, tests/test_orchestrator.py, tests/fakes.py (+ FakeLLM)
```

---

### Task 1: Texts and reply model

**Files:**
- Create: `app/texts.py`, `app/conversation/__init__.py` (empty), `app/conversation/reply.py`, `tests/test_texts.py`, `tests/test_reply.py`

**Interfaces:**
- Produces:
  - `Button(id: str, label: str)` frozen dataclass; `Reply(text: str, buttons: list[list[Button]] = [], undo_id: int | None = None)` frozen dataclass (buttons are rows).
  - `texts.py` constants: `ERRORS: dict[str, str]` keyed by every code in ERRORS.md that is user-visible (`STT_EMPTY`, `STT_FAILED`, `DISCOVERY_FAILED`, `LLM_UNAVAILABLE`, `LLM_INVALID_OUTPUT`, `INTENT_UNKNOWN`, `SEM_UNKNOWN_KEY`, `SEM_TYPE`, `SEM_UNSUPPORTED_OP`, `NOTION_4XX`, `NOTION_5XX`, `UNDO_EXPIRED`, `UNDO_FAILED`, `SESSION_EXPIRED`, `nothing_to_write`, `item_not_found`); `BTN_*` button labels (`BTN_CANCEL = "Отмена"`, `BTN_INBOX = "В разное"`, `BTN_UNDO = "Отменить"`, `BTN_CONFIRM = "Да"`, `BTN_OTHER = "Другое"`, `BTN_ADD_NEW = "Добавить как новое"`); `QUESTION: dict[QType, str]` templates; `DONE_*` execution templates; `INBOX_SAVED`, `INBOX_SAVED_EXPIRED`, `INBOX_FAILED`, `CANCELLED`, `UNDONE`, `SEARCH_EMPTY`, `SEARCH_HEADER`.
  - `format_execution(result: ExecutionResult, *, target_url: str | None) -> str` — describes the **actual Notion change**: verb by command type, target name, item title, then one `• <field>: <value>` line per `Written` (values rendered by `_label`: `Option.name`, lists joined, `DateRange` as `d.m.Y` / `d.m.Y HH:MM`, bool as Да/Нет), and a trailing link line when a URL is known (`ExecutionResult.url` or, when that is `None` — the append case — the target's own URL).
  - `format_search(result) -> str` — numbered list of up to 20 hits as `1. <title>` with the URL on the same line; `SEARCH_EMPTY` when there are none.
  - `format_question(q: Question, opts: list[AnswerOption], token: str) -> Reply` — question text from `QUESTION[q.type]` (with `field_name` / target name / `proposed` interpolated), one button per option (`a:<token>:o<i>`), then a final row with the type-specific extra buttons (`confirm`/`other`/`add_new`) plus `Отмена` and, when the inbox is enabled, `В разное`.
- Consumes: `Question`, `QOption` (`app.validation.policy`), `ExecutionResult`, `Written`, `SearchHit` (`app.commands.executor`), `DateRange` (`app.validation.semantic`).

- [ ] **Step 1: write the tests**
  - `tests/test_texts.py`: every `QType` has a template; every user-visible ERRORS.md code has a message; no template contains an unsubstituted `{`-placeholder after formatting with the documented kwargs; all strings non-empty.
  - `tests/test_reply.py`: `format_execution` for each of the five commands (create item with three written fields, update with one, create page, append with two paragraphs falling back to the target URL, search) — golden strings; `format_search` empty and with 3 hits; `format_question` for `target` (2 options + Отмена + В разное), `field_required` with options, `field_required` without options (free-text prompt, no option buttons), `date` (confirm + other), `item_not_found` (add-new button), `content_required`; every button id ≤ 48 bytes and ASCII.
  - Guard test: `grep`-style scan of `app/**/*.py` (excluding `texts.py`) finds no Cyrillic literal.
- [ ] **Step 2: implement `texts.py` and `reply.py`** until the tests pass.
- [ ] **Step 3:** `uv run ruff check .`, `uv run pytest -q`; commit `feat: Russian texts and transport-neutral reply model`.

---

### Task 2: Pending session state

**Files:**
- Create: `app/conversation/session.py`, `tests/test_session.py`
- Modify: `app/audit/store.py` (+ `expired_sessions`, `pop_session`), `tests/test_store.py`

**Interfaces:**
- Produces (all pydantic, `extra="forbid"`, JSON round-trippable):
  - `PendingField(field_id: str, name: str, type: str, status: str, value: Any = None, candidates: list[Any] = [], confidence: float = 1.0, source_text: str = "")` — `value`/`candidates` in `jsonvalue.to_json_value` shape (`{"id","name"}` for options, `{"start","end","granularity"}` for dates).
  - `PendingCandidate(target_id: str, confidence: float, item_page_id: str | None, item_text: str | None, content: str | None, search_query: str | None, fields: list[PendingField])`.
  - `AnswerOption(id: str, label: str, target_id: str | None = None, item_page_id: str | None = None, field_id: str | None = None, option_id: str | None = None, value: Any = None)` — the answer table for the question currently on screen: what each button *means* in Notion terms.
  - `PendingSession(chat_id: int, event_id: int, token: str, original_text: str, intent: str, intent_confidence: float, candidates: list[PendingCandidate], question: Question, options: list[AnswerOption], asked: list[str] = [], created_at: datetime, expires_at: datetime)` with `MAX_QUESTIONS = 3` module constant and helpers `best` (first candidate) and `is_exhausted` (`len(asked) >= MAX_QUESTIONS`).
  - `session_from_decision(chat_id, event_id, text, result: ValidationResult, decision: Decision, options: list[AnswerOption], *, now, ttl_s, asked: list[str]) -> PendingSession` — flattens `VCandidate`s into `PendingCandidate`s (Notion ids + JSON values only), generates the token (`secrets.token_hex(4)`).
  - `class SessionStore` wrapping `AuditStore`: `save(s)`, `get(chat_id, now) -> PendingSession | None` (invalid/unparseable JSON → delete + `None`, never raise), `drop(chat_id)`, `pop_expired(now) -> list[PendingSession]` (returns and deletes every session whose `expires_at <= now`, used by the inbox sweeper).
  - `AuditStore.expired_sessions(now) -> list[tuple[int, str]]` and `AuditStore.pop_session(chat_id) -> str | None` (read + delete under one lock).

- [ ] **Step 1: write the tests**
  - `tests/test_store.py`: `expired_sessions` returns only rows at/past expiry; `pop_session` returns the payload and removes the row; both thread-safe under the existing `RLock` (reuse the existing concurrency test style).
  - `tests/test_session.py`: `session_from_decision` on a `field_required` decision from `tests/helpers.py` keeps target/field/option **ids** and no `t…` context key anywhere in the serialised JSON (assert with a regex over `model_dump_json()`); round-trip through `SessionStore.save`/`get` preserves every field including `Question`; an expired session is not returned and is deleted; corrupt payload is deleted and returns `None`; `pop_expired` returns the expired ones only; `is_exhausted` after 3 answers.
- [ ] **Step 2: implement** `session.py` and the two store methods.
- [ ] **Step 3:** ruff + pytest; commit `feat: persistent pending sessions keyed by Notion ids`.

---

### Task 3: Answer resolver

**Files:**
- Create: `app/conversation/resolver.py`, `tests/test_resolver.py`

**Interfaces:**
- `rebuild_candidate(pc: PendingCandidate, snapshot: WorkspaceSnapshot, ctx: Context) -> VCandidate | None` — resolves `target_id` in the fresh snapshot (gone → `None`), re-keys the candidate against the fresh `Context` (`ctx.target_key`, `ctx.field_key`, `ctx.option_key`, `ctx.item_key`), rebuilds each `PendingField` into a `VField` (option no longer in the field → drop the field with a `SEM_UNKNOWN_KEY` issue; field gone → drop; date strings → `DateRange`), resolves `item_page_id` through `Target.item()` (gone → `item=None`).
- `result_from_session(s: PendingSession, snapshot, ctx) -> ValidationResult` — all candidates rebuilt (dropping unresolvable ones), ordered by confidence, `intent`/`intent_confidence` from the session.
- `apply_answer(s: PendingSession, option_id: str) -> tuple[PendingSession, str | None]` — returns the updated session and a control verb (`None` = re-evaluate, `"cancel"`, `"inbox"`, `"free_text"`): pure, id-based, no snapshot needed. Effects by question type:

| Question type | Button ids | Effect on the session |
|---|---|---|
| `target` | `o<i>` → `target_id` | keep only that candidate, confidence 1.0 |
| `intent_confirm` | `confirm` | `intent_confidence = 1.0` |
| `item` | `o<i>` → `item_page_id` | set on the best candidate |
| `item_not_found` | `add_new` | `intent = "create"`, title field ← `item_text`, `item_page_id = None` |
| `field_required` | `o<i>` → `(field_id, option_id)` | field status `value`, confidence 1.0 |
| `field_ambiguous` | `o<i>` → typed value | field status `value`, confidence 1.0, `candidates = []` |
| `date`, `field_confirm` | `confirm` | field confidence 1.0 |
| `date`, `field_confirm` | `other` | → `"free_text"` (the next text message answers it) |
| any | `cancel` / `inbox` | → `"cancel"` / `"inbox"` |

  Every applied answer appends `s.question.type + ":" + (field_id or "")` to `asked`; a question whose key is already in `asked` must never be re-asked — `next_question(decision, asked)` returns the first question not already answered, or `None`.
- `options_for(q: Question, candidate: VCandidate, result: ValidationResult, ctx: Context) -> list[AnswerOption]` — turns `Question.options` (context keys) into id-based `AnswerOption`s; this is the only place that translates keys → ids, and it runs while the generating context is still in hand.

- [ ] **Step 1: write the tests** (table-driven, one per row above, using `tests/helpers.py` to produce real `Decision`s):
  - rebuild after a *changed* snapshot: renamed target keeps id → still resolves; deleted select option → field dropped, others survive; deleted item → `item=None`; deleted target → `None`.
  - `apply_answer` for every row; `asked` accumulates; `next_question` skips answered ones; `cancel`/`inbox` verbs.
  - a full two-question sequence (target, then field_required) ends in an `EXECUTE` decision when re-evaluated by `Policy`.
  - `options_for` yields ids only (no `t…` key in any `AnswerOption` field) for `target`, `item`, `field_required` (with options), `field_ambiguous`.
- [ ] **Step 2: implement** `resolver.py`.
- [ ] **Step 3:** ruff + pytest; commit `feat: resolve button answers without a second LLM call`.

---

### Task 4: Inbox fallback target

**Files:**
- Create: `app/conversation/inbox.py`, `tests/test_inbox.py`
- Modify: `app/config.py`, `app/notion/descriptions.py`, `app/notion/snapshot.py`, `app/notion/discovery.py`, `.env.example`, `tests/test_config.py`, `tests/test_descriptions.py`, `tests/test_discovery.py`

**Interfaces:**
- `Settings.inbox_mode: Literal["auto", "button", "off"] = "auto"` (`INBOX_MODE`) — `auto`: save on every unresolvable path *and* offer the button; `button`: only when the user presses «В разное»; `off`: no inbox, no button. `Settings.inbox_target_id: str = ""` (`INBOX_TARGET_ID`) — optional explicit Notion page/data-source id that overrides the `targets.yaml` flag.
- `TargetMeta.inbox: bool = False` (persisted in `data/targets.yaml`, preserved by `Descriptions.ensure`, editable from the admin page in Plan 3b).
- `Target.is_inbox: bool = False` (last field, default preserves existing constructors); set in `Discovery._apply_meta` from the meta flag, or forced when `Target.id == settings.inbox_target_id` (Discovery gains an `inbox_target_id: str = ""` constructor argument). If several targets carry the flag, the lowest id wins and a WARNING is logged.
- `inbox_target(snapshot) -> Target | None` — the flagged target, or `None`.
- `inbox_command(target: Target, text: str, *, note: str | None, now: datetime, tz: str) -> Command` — for a page target: `AppendBlocks(page_id=target.id, page_title=target.name, paragraphs=[f"{now:%Y-%m-%d %H:%M} — {text}"] + ([note] if note else []))`; for a database target: `CreateItem` with the title property set to `text` (title property missing → `ValueError`, caller degrades to the plain error reply). Text is capped at `MAX_TEXT = 4000` chars, the note at 200.
- The inbox target stays visible to the LLM as an ordinary target (the user may deliberately say «запиши в разное»); the prompt is not told it is a fallback, so the model cannot use it as an escape hatch.

- [ ] **Step 1: write the tests**
  - `tests/test_config.py`: defaults; `INBOX_MODE=bogus` rejected.
  - `tests/test_descriptions.py`: `inbox: true` survives a load→ensure→save round trip; a yaml without the key loads as `False`.
  - `tests/test_discovery.py`: flag from yaml sets `is_inbox`; `inbox_target_id` override wins over the yaml flag; two flagged targets → one winner + a warning.
  - `tests/test_inbox.py`: page target → one paragraph, with note → two; timestamp format; 5000-char text truncated to 4000; database target → `CreateItem` with only the title property; database without a title property → `ValueError`; `inbox_target` returns `None` when nothing is flagged.
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: inbox fallback target for unresolvable messages`.

---

### Task 5: Orchestrator

**Files:**
- Create: `app/conversation/orchestrator.py`, `tests/test_orchestrator.py`
- Modify: `tests/fakes.py` (+ `FakeLLM` returning a queued `Interpretation` or raising a queued exception, recording the contexts it saw)

**Interfaces:**

```python
class Orchestrator:
    def __init__(self, settings: Settings, discovery: Discovery, builder: ContextBuilder,
                 llm: LLMClient, validator: SemanticValidator, policy: Policy,
                 executor: Executor, store: AuditStore, sessions: SessionStore,
                 clock: Callable[[], datetime] = now_utc) -> None: ...

    async def handle_text(self, chat_id: int, user_id: int, text: str, *,
                          kind: str = "text", transcript: str | None = None) -> Reply: ...
    async def handle_callback(self, chat_id: int, user_id: int, data: str) -> Reply: ...
    async def undo(self, chat_id: int, execution_id: int | None = None) -> Reply: ...
    async def cancel(self, chat_id: int) -> Reply: ...
    async def flush_expired_sessions(self) -> int: ...   # sweeper; Plan 3b schedules it
```

**Pipeline (`handle_text`)**

1. Open the `events` row (`kind`, `raw_input`, `transcription`); start the duration clock.
2. `sessions.get(chat_id, now)`:
   - a **live** session → this text is a free-text answer: build the context with `pending={"вопрос": …, "цель": <target name>, "исходный_текст": s.original_text}` (names only, never ids or keys) and send the LLM `s.original_text + "\n" + text`; the fresh interpretation **replaces** the session (F4/F5), carrying `asked` forward.
   - an **expired** session was just dropped by `get` → inbox-save its `original_text` (mode `auto`) and prepend `INBOX_SAVED_EXPIRED` to whatever this message produces.
3. `snapshot = await discovery.get()` → on `NotionError`/network failure: `DISCOVERY_FAILED` (the discovery layer already falls back to a stale snapshot < 1 h; only a hard failure reaches here). No inbox (Notion is unreachable).
4. `ctx = builder.build(snapshot, now, pending)`; empty context (no targets) → `DISCOVERY_FAILED`.
5. `schema = build_schema(ctx)`; `interp, trace = await llm.interpret(text, ctx, schema)`; audit `llm_model`, `llm_context`, `llm_response`, token counts.
   `LLMUnavailable` → `LLM_UNAVAILABLE` + inbox; `LLMInvalidOutput`/`LLMContextOverflow` → `LLM_INVALID_OUTPUT` + inbox.
6. `result = validator.validate(interp, ctx, snapshot)`; `decision = policy.evaluate(result)`; audit both.
7. Dispatch:
   - **EXECUTE** → `build_command(decision.candidate, snapshot)` → `executor.run(cmd)` → audit `command`, `executed`, `notion_page_id` → `add_execution` when `result.undo` is not `None` → `Reply(format_execution(...), [[Button(f"u:{exec_id}", BTN_UNDO)]], undo_id=exec_id)`. A `NotionError` here → `NOTION_4XX`/`NOTION_5XX` + inbox (the write did not happen, the text would otherwise be lost).
   - **CLARIFY** → `q = next_question(decision, asked)`; when `None` or the session is exhausted → inbox fallback; else `options_for(...)` → `session_from_decision(...)` → `sessions.save` → `format_question`.
   - **REJECT** → inbox fallback with the reason code; `item_not_found` and `nothing_to_write` keep their carried question and are asked as a CLARIFY instead (the `add_new` button makes `item_not_found` actionable — this replaces the "no handler yet" note in ARCHITECTURE §8).
8. Close the `events` row (`decision`, `duration_ms`, `error`).

**`handle_callback`** parses `a:<token>:<opt>` / `u:<id>`; unknown prefix, unknown session, or a token mismatch → `SESSION_EXPIRED`. Otherwise `apply_answer` → verb dispatch (`cancel` → drop + `CANCELLED`; `inbox` → inbox-save `original_text` + drop; `free_text` → keep the session, reply "введите значение"; `None` → rebuild the candidate against a fresh snapshot, re-evaluate with `Policy`, and re-enter step 7 — no LLM call). Each callback opens its own `events` row with `kind="callback"`.

**Inbox fallback helper** — `async def _to_inbox(self, chat_id, text, code, event_id) -> Reply | None`: returns `None` when the mode is `off`/`button` or no target is flagged (the caller then replies with the plain `ERRORS[code]`); otherwise builds and runs the command, audits it as an execution with an undo record, and replies `INBOX_SAVED` (`ERRORS[code]` + « Сохранил в «<target>»: <url>») with an Undo button. A failure inside the fallback is caught, audited, and degraded to `ERRORS[code] + INBOX_FAILED`.

- [ ] **Step 1: `tests/fakes.py` — `FakeLLM`** with `queue(interp)` / `queue_error(exc)`, recording `(text, context_payload, schema)` per call, and a `calls` counter (used to assert that button answers make **no** LLM call).
- [ ] **Step 2: write the e2e tests** (`FakeLLM` + `FakeNotionProvider` + real everything else + temp SQLite), one per flow:
  - F1 create, unambiguous → EXECUTE, reply names the target and the written fields, one execution row, undo button present.
  - F2 target ambiguous → CLARIFY with two buttons; pressing one → EXECUTE; `FakeLLM.calls == 1`.
  - F3 required field missing → CLARIFY; option button → EXECUTE with that option written.
  - F4 free-text answer → second LLM call whose context carries a `pending` block containing **no** `t…` key and no Notion id.
  - F5 unrelated message while pending → old session replaced, new request handled.
  - F6/F7 update by reference / item ambiguous → button → `UpdateItem` with the right page id.
  - F8 item not found → `add_new` button → `CreateItem` with `item_text` as the title.
  - F9 append → reply link falls back to the target URL.
  - F10 create sub-page. F11 search → formatted hits, no execution row, no undo button.
  - F12 low-confidence date → confirm button → EXECUTE; `other` → free-text follow-up.
  - F14 infrastructure: `LLMUnavailable`, `LLMInvalidOutput`, `NotionError` on execute, discovery failure — each replies with its code and (except discovery) lands in the inbox.
  - Inbox: REJECT (`intent_unknown`) → appended, reply carries the link and an Undo button that deletes the block; mode `button` → not saved automatically but the button is present; mode `off` → no button, plain rejection; no flagged target → plain rejection; inbox write fails → `INBOX_FAILED`.
  - Sessions: expired session + new message → old text goes to the inbox and the new message is handled; `flush_expired_sessions` saves and clears; stale token → `SESSION_EXPIRED`; 4th question → inbox.
  - Undo: button → `mark_undone`, second press → `UNDO_EXPIRED`/already undone; `/undo` with no execution → `UNDO_EXPIRED`.
  - Audit: every one of the above wrote exactly one `events` row with a non-null `decision` and `duration_ms` (a shared assertion helper).
- [ ] **Step 3: implement `orchestrator.py`** until they pass.
- [ ] **Step 4:** ruff + pytest; commit `feat: conversation orchestrator with clarification, undo and inbox fallback`.

---

### Task 6: Documentation sync

**Files:** `documentation/ARCHITECTURE.md`, `documentation/DATA_MODEL.md`, `documentation/FLOWS.md`, `documentation/ERRORS.md`, `documentation/NOTION_SETUP.md`, `documentation/IMPLEMENTATION_PLAN.md`, `.env.example`

- [ ] **Step 1:** ARCHITECTURE — §1 decisions table: new row "Unresolvable messages → appended to a user-flagged inbox target, never dropped"; §3 module layout: real `conversation/` files; §4 pipeline: the inbox branch; §7: the answer table, `MAX_QUESTIONS`, the token, the «В разное» button, `item_not_found` now handled; §8: remove "MVP has no handler for that question type yet"; §12: the admin page also picks the inbox target (Plan 3b).
- [ ] **Step 2:** DATA_MODEL — §2 `targets.yaml` gains `inbox`; §4 replaces the `PendingSession` placeholder with the real models and the `AnswerOption` table; §7 documents `INBOX_MODE`, `INBOX_TARGET_ID`.
- [ ] **Step 3:** FLOWS — new **F17 «Не понял → в разное»** and **F18 «Вопрос устарел → в разное»** with the exact Russian replies; update F8 (add-new button) and F4 (pending block contents).
- [ ] **Step 4:** ERRORS — add `INBOX_SAVED` (informational), `INBOX_FAILED`, `INBOX_NOT_CONFIGURED`; note which codes trigger the fallback.
- [ ] **Step 5:** NOTION_SETUP — a short section: create a page named e.g. «Разное», make sure the integration can see it, then flag it (admin page in Plan 3b, or `INBOX_TARGET_ID` in `.env` meanwhile).
- [ ] **Step 6:** IMPLEMENTATION_PLAN — mark T-012/T-040/T-041 done, T-051 partially (orchestrator side), record the inbox as a scope addition.
- [ ] **Step 7:** commit `docs: conversation layer and inbox fallback`.

---

## Verification

- `uv run pytest -q` — all green, no new `integration` tests.
- `uv run ruff check .` — clean.
- No Cyrillic outside `app/texts.py` (test enforced).
- No `t\d` context key in any persisted session payload (test enforced).
- Button answers never call the LLM (test enforced).
- Every handled message writes exactly one `events` row (test enforced).

## Out of scope (Plan 3b)

Telegram handlers/auth/keyboards, voice + Whisper, `main.py` wiring and startup checks, the admin page (including the inbox picker), structured logging with `event_id`, README, security/QA tests, release 0.1.0.
