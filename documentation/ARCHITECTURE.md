# Architecture

Source spec: [../Local Telegram → Notion Assistant — Technical Specification.md](../Local%20Telegram%20%E2%86%92%20Notion%20Assistant%20%E2%80%94%20Technical%20Specification.md).
This document fixes the decisions the spec leaves open. Where they differ, this document wins.

## 1. Decisions made after spec review

| Topic | Decision | Why |
|---|---|---|
| Writable scope | Everything the Notion integration can see is a target. No allow/deny lists. | User request. Notion share settings are the single access control. |
| Item lookup for update/append | LLM sees existing item titles (capped) and picks an item by short key. App only checks the key exists. | "LLM as classifier": no second fuzzy-match step, fewer questions. |
| Target/field/item references | App assigns short keys per request (`t1`, `t1.f3`, `t1.i12`). LLM output schema uses `enum` of those keys. | Prevents invented IDs, saves tokens, grammar-enforced by Ollama. |
| Confirmation | LOW and MEDIUM risk execute automatically when policy passes. Reply carries an **Undo** button for `UNDO_WINDOW_SECONDS`. HIGH risk not in MVP. | Minimal questions. |
| Schema freshness | Workspace snapshot fetched per incoming message, cached `SCHEMA_CACHE_TTL` (60 s) so clarification round trips reuse it. | Always fresh, cheap at personal usage. |
| Descriptions | `data/targets.yaml` keyed by Notion id, edited through a local admin page on `127.0.0.1:ADMIN_UI_PORT`. Notion data source `description` used as default. | Minimal host UI, human-editable file. |
| Reply language | Russian. All user-facing strings live in one module. | MVP. |
| Admin UI stack | Stdlib `http.server` in a thread, one embedded HTML page, two JSON endpoints. | No extra dependencies for a rarely used page. |
| Remote AI later | Ollama and speech behind protocols; `OLLAMA_BASE_URL`, optional `WHISPER_SERVER_URL`. | User may add a dedicated AI machine. |
| Package/tooling | `uv`, Python 3.12 (uv-managed), `ruff`, `pytest`, `pydantic-settings`. | 3.13 wheel coverage for ctranslate2 is uneven. |
| Notion API | Direct REST via `httpx`, `Notion-Version: 2025-09-03` (data sources). | Spec; official SDK adds little. |
| Unresolvable messages | Appended to a user-flagged inbox target (`targets.yaml` `inbox: true`, or the `INBOX_TARGET_ID` override, which wins), never dropped. | Nothing the pipeline gives up on should vanish. The target stays ordinary for the LLM — not hidden, not mentioned in the prompt — so the model can't learn to use it as an escape hatch instead of classifying. |

## 2. Runtime topology

Single `asyncio` process on the host machine, one event loop for the whole run
(python-telegram-bot's own — `Application.run_polling()`; see `app/main.py`'s module docstring for
why nothing here is ever given a second, throwaway loop of its own):

```text
python-telegram-bot (long polling), one asyncio event loop for the whole process
   │
   ├─ app.main.post_init_checks (runs once, inside this same loop, before polling starts):
   │     Ollama /api/tags, Notion /v1/users/me, initial discovery, admin server start
   ├─ telegram/handlers.py: text, voice, callback, command handlers → Orchestrator
   ├─ speech: faster-whisper, loaded lazily on the first voice message, run via
   │     asyncio.to_thread (GPU if available, else CPU — see §10)
   ├─ llm: httpx → Ollama /api/chat  (OLLAMA_BASE_URL)
   ├─ notion: httpx → api.notion.com (NOTION_TOKEN never leaves this layer)
   ├─ sqlite: data/bot.sqlite  (audit, pending sessions, undo records)
   ├─ Sweeper: one background asyncio task, started after a clean post_init, that calls
   │     Orchestrator.flush_expired_sessions every SESSION_TTL_S / 3 seconds — the net that
   │     rescues a question nobody answered even if the chat never sends a follow-up message
   │     (FLOWS.md F18 step 4)
   └─ admin: stdlib http.server on a daemon thread, bound to 127.0.0.1:ADMIN_UI_PORT (§12)
```

No webhooks, no Docker, no public port. `app.main.build()` constructs every collaborator above
exactly once and wires the two Telegram lifecycle hooks: `post_init` (the async startup checks,
then start the Sweeper) and `post_shutdown` (stop the Sweeper, stop the admin server, close the
sqlite connection) — see §15 and `documentation/ERRORS.md`'s "Startup checks" for the exact,
ordered list and exit codes.

## 3. Module layout

```text
notion_ai_bot/
├── app/
│   ├── config.py               Settings (pydantic-settings, .env)
│   ├── main.py                 wiring, startup checks, Sweeper, process entry point
│   ├── logging_setup.py        event_id-tagged structured logging; httpx/httpcore/ExtBot floor
│   ├── version.py              reads the running version (pyproject.toml, or a shipped VERSION)
│   ├── texts.py                all Russian user-facing strings
│   ├── telegram/
│   │   ├── handlers.py         message/voice/callback/command handlers → Orchestrator
│   │   ├── keyboards.py        Reply → telegram.InlineKeyboardMarkup
│   │   └── auth.py             allowlist filter (declarative gate + the one manual callback check)
│   ├── speech/
│   │   ├── base.py             SpeechToText protocol, SpeechEmpty/SpeechError
│   │   └── whisper_local.py    faster-whisper implementation (lazy load, CUDA→CPU fallback, §10)
│   ├── llm/
│   │   ├── base.py             LLMClient protocol, LLMTrace, error types
│   │   ├── ollama.py           httpx client, structured output
│   │   ├── context.py          ContextBuilder: snapshot → keyed compact context
│   │   ├── prompts.py          system prompt (ru), few-shot examples
│   │   └── output_schema.py    dynamic JSON schema + Pydantic models per request
│   ├── interpretation/
│   │   └── models.py           Interpretation, Candidate, FieldValue (discriminated union)
│   ├── notion/
│   │   ├── provider.py         NotionProvider protocol
│   │   ├── direct.py           DirectNotionProvider (REST)
│   │   ├── discovery.py        search + data source + items → WorkspaceSnapshot
│   │   ├── snapshot.py         WorkspaceSnapshot, Target, Field, Item dataclasses
│   │   ├── descriptions.py     targets.yaml read/write
│   │   ├── mapper.py           Command → Notion payload
│   │   ├── props.py            Notion property JSON → plain text (titles, rich text)
│   │   └── errors.py           NotionError/NotionUnavailable
│   ├── validation/
│   │   ├── semantic.py         checks against snapshot
│   │   └── policy.py           thresholds → EXECUTE / CLARIFY / REJECT
│   ├── commands/
│   │   ├── models.py           CreateItem, UpdateItem, CreatePage, AppendBlocks, Search, …
│   │   ├── builder.py          VCandidate + snapshot → Command (build_command)
│   │   ├── jsonvalue.py        JSON-safe typed-field-value conversion (shared with policy.py)
│   │   └── executor.py         runs commands via provider, records undo info
│   ├── conversation/
│   │   ├── reply.py            Button/Reply model; formats questions, execution + search text
│   │   ├── session.py          PendingSession/SessionStore: persisted clarification state
│   │   ├── resolver.py         apply_answer/result_from_session: button answers, no LLM call
│   │   ├── inbox.py            builds the Command that saves unresolved text to the flagged target
│   │   └── orchestrator.py     one entry point: handle_text/handle_callback/undo/cancel
│   ├── audit/
│   │   ├── store.py            SQLite schema + repository (events, sessions, executions)
│   │   └── migrate.py          migration runner: discover/pending/apply, checksum guard
│   └── admin/
│       ├── server.py           stdlib HTTP server thread (§12)
│       └── page.html           editor page (targets, required flags, inbox picker)
├── tools/
│   ├── benchmark_llm.py        run fixtures against N models, report accuracy/latency
│   ├── discover.py             dump workspace snapshot to stdout
│   ├── migrate.py              apply/inspect migrations from the command line
│   └── sample_workspace.py     fixed workspace used by tests and the benchmark
├── migrations/                 NNNN_name.sql, applied only by tools/migrate.py or the installer
├── deploy/                     install.ps1, run.ps1, update.ps1, *_wizard.ps1 (RELEASE.md)
├── release.py, apply_update.py, release.cmd, update.cmd
├── tests/
├── data/                        runtime files (gitignored): bot.sqlite, targets.yaml
├── documentation/
├── pyproject.toml
└── .env.example
```

`app/speech/whisper_remote.py` (a remote-Whisper HTTP client) does not exist yet — remote speech
recognition is out of scope for this plan (see the Plan 3b task briefs' "Out of scope"); today
`WHISPER_DEVICE=cpu` is the only way to run transcription without a GPU, not a way to move it to
another machine. `OLLAMA_BASE_URL` has no such limitation — it already works against Ollama
running on a different host (README.md).

## 4. Request pipeline

```text
Telegram update
  → auth (allowlist) → reject silently if not allowed
  → voice? download → SpeechToText → text (audited, not echoed)
  → Orchestrator.handle_text(chat_id, user_id, text)         (or handle_callback/undo/cancel)
      → session = pending for chat? (then this is a clarification answer, see §7)
      → snapshot = Discovery.get(ttl)                      (Notion)
      → ctx = ContextBuilder.build(snapshot, now, pending)  (pending = §7's `pending` block, from session)
      → schema = OutputSchema.for_context(ctx)             (dynamic JSON schema)
      → raw = LLMClient.interpret(text, ctx, schema)        (Ollama, temp 0)
      → interp = Interpretation.model_validate(raw)        (Pydantic)
      → sem = SemanticValidator.check(interp, snapshot)
      → decision = Policy.evaluate(interp, sem)
      → EXECUTE: cmd = CommandBuilder.build(best, snapshot)
                 result = Executor.run(cmd)                 (Notion)
                 reply + Undo button; audit
                 Notion error on execute → inbox fallback instead of a bare error (text not lost)
        CLARIFY: session.save(question); ask with buttons; audit
                 MAX_QUESTIONS (3) reached without a resolution → inbox fallback
        REJECT:  no candidate, or invalid/unavailable LLM output → inbox fallback; audit
```

Callback payloads: `a:<token>:<option_id>` (`apply_answer` → `result_from_session`, no LLM call),
`u:<execution_id>` (undo), `i:<event_id>` (save that event's own text to the inbox — the button
offered on a reply that saved nothing). A token that doesn't match the chat's live session, or an
unknown prefix, replies `SESSION_EXPIRED`.

`Orchestrator._text` runs discovery **before** it looks the session up, and that order is
deliberate: looking first would pop an expired session (the lookup is destructive — it is what
rescues the text into the inbox) and only then discover that Notion is unreachable, with nowhere
to write it. Since the session row is the only copy of that text, the message would be gone. Do
not reorder these two for the sake of skipping a snapshot fetch on the error path.

Free text while a session is pending → the LLM is re-run once, with a `pending` block (question
text, target name, original text — never a Notion id or context key) added to the context and
`session.original_text + "\n" + new_text` as the prompt; the fresh interpretation replaces the
session outright, whether it answers the question or turns out to be a new request (F4, F5).
`MAX_QUESTIONS = 3` bounds button round-trips only — a free-text answer is indistinguishable from
a fresh request and is never refused for exceeding it. It is what bounds the *number* of answers;
what bounds their *size* is `orchestrator.MAX_PROMPT` (4000 chars, the same ceiling the validator
and the inbox put on one piece of text), which caps the concatenation each answer grows — without
it a user who keeps answering in free text eventually ends the conversation in
`LLMContextOverflow`. The capped text is what the session stores and what a fallback page title or
search query is built from (`commands/builder.py:_one_line` folds it back onto one line).

**Inbox fallback**: a message the pipeline could not otherwise resolve — an invalid/unavailable
LLM response, a REJECT, a Notion error during execute, an expired unanswered session, the
«В разное» button, or a clarification budget (`MAX_QUESTIONS`) that ran out — is appended to the
one Notion target the user flagged as the inbox (`targets.yaml` `inbox: true`, or the
`INBOX_TARGET_ID` override, which wins and logs a WARNING when it matches no target) instead of
being dropped. Never triggered by a bare discovery failure (nothing to write to) or after a
successful write. `INBOX_MODE` controls when: `auto` (default) saves immediately and still offers
the button; `button` saves only on a press; `off` disables it entirely.

## 5. Workspace snapshot and discovery

`Discovery.refresh()`:

1. `POST /v1/search` paginated, no filter → all pages and data sources visible to the integration.
2. For each data source: `GET /v1/data_sources/{id}` → properties, title, description, parent database id.
3. For each data source: `POST /v1/data_sources/{id}/query` with `sorts=[last_edited_time desc]`, `page_size=ITEMS_PER_TARGET` (default 15) → items (id, title, status/checkbox if present).
4. Relation properties: options = items of the related data source (already fetched in step 3 if visible; otherwise one extra query, capped).
5. Pages (not inside a data source): included as page targets with title, id, parent chain. Children come from search results (`parent.page_id`).
6. Merge `targets.yaml` descriptions (override) and Notion descriptions (default).

Output: `WorkspaceSnapshot` (see [DATA_MODEL.md](DATA_MODEL.md)). Cached in memory with `fetched_at`; `Discovery.get(ttl)` returns cached if younger than `SCHEMA_CACHE_TTL`.

Rate limit: Notion allows ~3 req/s. Discovery of N data sources costs `1 + 2N` requests, run with a semaphore of 3. For 5–10 targets, 1–3 s.

Supported property types for write: `title, rich_text, select, multi_select, status, date, checkbox, number, url, relation`. Others are shown to the LLM as read-only context and are never written.

## 6. LLM context and output schema

### Context (sent as user message, compact JSON)

```json
{
  "now": "2026-09-09T18:40:00+03:00", "tz": "Europe/Tallinn", "weekday": "вторник",
  "targets": [
    {"key": "t1", "kind": "database", "name": "Покупки", "path": "Дом / Покупки",
     "description": "Список покупок…", "ops": ["create", "search", "update"],
     "fields": [
       {"key": "t1.f1", "name": "Название", "type": "title", "required": true},
       {"key": "t1.f2", "name": "Магазин", "type": "select", "options": {"t1.f2.o1": "Rimi", "t1.f2.o2": "Prisma"}},
       {"key": "t1.f3", "name": "Куплено", "type": "checkbox"}
     ],
     "items": {"t1.i1": "Хлеб", "t1.i2": "Молоко (куплено)"}},
    {"key": "t2", "kind": "page", "name": "Идеи", "path": "Идеи", "description": "…",
     "ops": ["append", "create", "search"],
     "children": {"t2.i1": "Отпуск 2027"}}
  ]
}
```

Extraction covers every writable field of the chosen target, always. `required` only controls whether the bot must ask when the message did not mention the field. Notion exposes no mandatory-property flag except the title, so `required` defaults to the title and other fields are marked in `targets.yaml` (admin page). Keys are regenerated per request; the mapping key → Notion id is held by the app.

### Output (grammar-enforced by Ollama `format`)

Per request the app builds a JSON schema where `target`, `item`, field keys and select/relation option keys are `enum`s from the context. Structure:

```text
Interpretation
  intent: {value: create|update|append|search|unknown, confidence: 0..1}
  candidates: 1..3 × Candidate           ordered best first
  notes: string                          free text for the audit log only
Candidate
  target: enum(target keys)
  confidence: 0..1
  item: enum(item keys of that target) | null     update/append target item
  item_candidates: [enum]                         when several items plausible (max 8, else [])
  fields: {field_key: FieldValue}                 all writable fields of the target, always present
  content: string | null                          append / page body text
  search_query: string | null
FieldValue (discriminated by status)
  not_mentioned
  explicit_null
  value      {value, confidence, source_text}     value typed per field type
  ambiguous  {candidates: [..], source_text}
```

Field `value` typing: `title/rich_text/url` string; `number` number; `checkbox` bool; `date` `{start: ISO date or datetime, end: ISO | null}`; `select/status` enum(option keys); `multi_select/relation` list of enum(option keys).

If grammar-enforced `oneOf` proves slow on the chosen model, fallback is a looser schema (strings instead of enums) with the same Pydantic validation in the app; the app behaviour is identical because semantic validation rejects unknown keys anyway.

The semantic validator (Plan 2b) re-checks every key against the context, dedupes candidates by target, types values per field, and caps lengths — the grammar is a first line, not the only line.

### Prompt

System prompt (Russian): role = classifier and extractor, not an agent. Rules: pick only from provided keys; return every writable field with a status; never guess a select option; dates resolved from `now`; return alternatives only when plausible; for `update`/`append`, `item` must come from `items`; thinking disabled (`think: false` for qwen3).

## 7. Conversation state

```text
IDLE ──text──▶ INTERPRETING ──EXECUTE──▶ IDLE (+undo record)
                    │
                    ├──REJECT──▶ IDLE (+ inbox fallback: saved if a target is flagged, else [В разное] offered)
                    │
                    └──CLARIFY──▶ WAITING (session saved, its `token` embedded in every button payload)
                                    │
                        button ─────┤─▶ apply_answer → result_from_session → EXECUTE|CLARIFY|REJECT (no LLM call)
                        free text ──┤─▶ LLM with the `pending` block in context → validate → …
                        [В разное] ─┤─▶ inbox fallback, session dropped
                        MAX_QUESTIONS (3) reached ─┤─▶ inbox fallback (budget exhausted)
                        timeout ────┘─▶ IDLE (+ inbox fallback on the next message, or the sweeper)
```

**One chat's turns are serialised.** Every entry point (`handle_text`, `handle_callback`,
`undo`, `cancel`, and each chat's own rescue inside `flush_expired_sessions`) takes that chat's
`asyncio.Lock` for the whole turn, so no two turns of one chat overlap; different chats stay fully
concurrent, and the lock is dropped again once no turn holds it. It is not a nicety: every guard
in the orchestrator is a read, an `await`, then a write — `events.executed` before an inbox save,
`executions.undone` before an undo, the session row across a save — and a double-tapped Telegram
button arrives as two callbacks ~100 ms apart in two concurrent handlers, where both reads would
see the state from before either write and the message would be saved (or reverted) twice. The
sweeper takes each chat's lock around that chat's own pop and rescue, never one lock for the whole
sweep.

One pending session per chat, keyed by `chat_id`. `MAX_QUESTIONS = 3` bounds *button* round-trips
only: once three questions have been asked and answered by button, a fourth CLARIFY falls back to
the inbox instead of asking again. A free-text answer is exempt — it re-runs the LLM and is
indistinguishable from a fresh request, so it is never refused for exceeding the budget. A new
unrelated message while WAITING is handled exactly like a free-text answer to the pending
question: the LLM sees both in one call and its fresh interpretation replaces the session outright
— continuing the same request, or starting an unrelated one, depending only on what it returns.

Clarification question types (each rendered by `conversation/reply.py:format_question`):

| Type | Trigger | Buttons |
|---|---|---|
| `target` | target margin < threshold | one per candidate target + Отмена (+ В разное) |
| `intent_confirm` | only trigger is `intent.confidence < POLICY_INTENT_MIN`, exactly one candidate | Да + Отмена (+ В разное) |
| `item` | update: item missing or several candidates; append: several candidates (no item means append to the page itself) | one per candidate item (max 8) + Отмена (+ В разное) |
| `field_required` | required field `not_mentioned` | options for select/status/relation; free-text prompt for others |
| `field_ambiguous` | field status `ambiguous` | one per candidate value |
| `date` | date confidence < `POLICY_DATE_MIN` | Да / Другое (free text) + Отмена (+ В разное) |
| `item_not_found` | update: item not in target's items and no `item_candidates` | Добавить как новое + Отмена (+ В разное) — REJECT, not a full CLARIFY round-trip, but answerable: the button flips the intent to `create` and re-validates (F8) |
| `nothing_to_write` | update: item resolved but no field `value`/`explicit_null`/`ambiguous` | Отмена (+ В разное); answer is free text only — REJECT, not a clarification round-trip |

Every question type's trailing row also carries [В разное] whenever an inbox target is flagged
(mode `auto` or `button`) — pressing it saves the session's `original_text` and drops the session.

One question per message. Several open issues are asked in the `_ORDER` from `policy.py`: `target → intent_confirm → item → item_not_found → field_required → field_ambiguous → date → field_confirm → content_required → nothing_to_write` (the last two of these — `item_not_found`, `nothing_to_write` — are REJECT-only single questions, never part of a multi-question CLARIFY).

## 8. Policy engine

Deterministic (`validation/policy.py`). `Thresholds.from_settings` reads `POLICY_INTENT_MIN`, `POLICY_TARGET_MIN`, `POLICY_TARGET_MARGIN`, `POLICY_FIELD_MIN`, `POLICY_DATE_MIN` from `Settings` (§7 of DATA_MODEL.md); no threshold is hardcoded in the policy itself.

```text
REJECT if intent = unknown, or no candidate survived semantic validation (SemanticValidator issues)
      or (update) and item not in target's items and no item_candidates      # carries the candidate + an item_not_found Question, see below
      or (update) and item resolved but no field status = value/explicit_null/ambiguous # carries the candidate + a nothing_to_write Question
CLARIFY if intent.confidence < POLICY_INTENT_MIN      # intent_confirm instead of target when this is the only trigger and there is exactly one candidate
      or best.confidence < POLICY_TARGET_MIN
      or (best.confidence - second.confidence) < POLICY_TARGET_MARGIN   (only when second exists)
      or (update|append) with item null and item_candidates non-empty
      or (create) with a required field not_mentioned / explicit_null
      or any field status = ambiguous
      or a date-typed field value with confidence < POLICY_DATE_MIN
      or any other field value with confidence < POLICY_FIELD_MIN
      or (append) with no content
EXECUTE otherwise
```

Question order when several apply (one asked per message, `_ORDER` in `policy.py`): `target → intent_confirm → item → item_not_found → field_required → field_ambiguous → date → field_confirm → content_required → nothing_to_write`.

`update` with no resolvable item is a REJECT, not a CLARIFY, when there are no `item_candidates` to pick from — but unlike other REJECTs it still carries `Decision.candidate` (the resolved target/fields) and one `Question("item_not_found", ...)`, purely so the conversation layer can offer "add as new item" without re-running the LLM. It is handled: `resolver.options_for`/`apply_answer` attach one **Добавить как новое** button; pressing it flips the intent to `create`, seeds the title field from the unmatched text, drops the stale item id, and re-validates — turning the failed update into a create without a second LLM call (FLOWS.md F8).

An `explicit_null` on a `status`-type field is dropped before it reaches the policy: `SemanticValidator` turns it back into `not_mentioned` and appends a `SEM_STATUS_CLEAR` issue (warning-level, not user-visible — the Notion API has no way to clear a status property), so the field is simply left unwritten rather than blocking or clarifying.

Risk classes: `RISK_BY_INTENT` = `create`, `append`, `search` → LOW; `update` → MEDIUM. `Decision.risk` records this on every EXECUTE/CLARIFY (and on the `item_not_found` REJECT) for the audit log; both LOW and MEDIUM auto-execute in MVP with no extra confirmation step. Undo available for `create` (archive page), `update` (restore previous property values), `append` (delete appended blocks). Undo button expires after `UNDO_WINDOW_SECONDS`.

## 9. Commands and Notion mapping

| Command | Fields | Notion call |
|---|---|---|
| `CreateItem` | data_source_id, target_name, properties | `POST /v1/pages` parent `data_source_id` |
| `UpdateItem` | page_id, target_name, item_title, properties | `PATCH /v1/pages/{id}` |
| `CreatePage` | parent_page_id, target_name, title, body | `POST /v1/pages` parent `page_id` + paragraph blocks |
| `AppendBlocks` | page_id, target_name, page_title, paragraphs | `PATCH /v1/blocks/{id}/children` |
| `Search` | data_source_id or None, target_name, title_property or None, query, filters | `POST /v1/data_sources/{id}/query` compound filter, or `POST /v1/search` |
| `ArchivePage` (undo) | page_id | `PATCH /v1/pages/{id}` `archived: true` |
| `DeleteBlocks` (undo) | block_ids | `DELETE /v1/blocks/{id}` |

`Search.filters` is built from the validated fields with status `value` (select/status/multi_select/relation/checkbox — see DATA_MODEL.md §5); `mapper.py:search_filter` composes them into one Notion filter object: `select`/`status` → `equals`, `multi_select`/`relation` → one `contains` per value, `checkbox` → `equals`, plus an optional title `contains` from `query`. Zero conditions → unfiltered; more than one → `{"and": [...]}`.

Commands hold Notion ids resolved by the app from keys. `mapper.py` is the only place that builds Notion JSON. LLM output never reaches it.

`Target.operations` vocabulary: databases `{create, update, search}`, pages `{create_page, append, search}`; the command classes above are the executable form of those operations.

## 10. Speech

`app/speech/whisper_local.py:WhisperLocal`, faster-whisper, model `WHISPER_MODEL` (default
`large-v3-turbo`), `compute_type=int8`, `device=auto` (CUDA if available, else CPU), `language=ru`
hint (configurable), `vad_filter=True`. Transcript is stored in audit only; the reply describes
the actual Notion change, never the transcript (FLOWS.md F13, ERRORS.md `STT_EMPTY`/`STT_FAILED`).

* **Lazy load.** The model is *not* loaded at startup (`app.main.post_init_checks` deliberately
  never touches `app.speech`) — only on the first voice message, inside the worker thread
  described below, and then kept for the life of the process. A user who never sends a voice
  message never pays the load cost at all.
* **One `threading.Lock` serialises every call**, not just the load. `transcribe()` runs the
  whole thing (`_transcribe_sync`) via `asyncio.to_thread`, so it executes on a worker thread, not
  the event loop — a plain `asyncio.Lock` would not help there. The lock covers the load *and*
  the decode because one GPU cannot decode two clips at once anyway, and narrowing it to just the
  load would reopen two races: two concurrent calls disagreeing about which device is current
  mid-fallback, and a fallback on one call racing an in-flight transcription on another.
* **CUDA → CPU fallback, remembered for the process.** A failure — whether raised while loading
  the model or only while decoding the first segments — logs one WARNING naming the original
  error, flips the instance to CPU, and retries once; a second failure raises `SpeechError`
  (`STT_FAILED`). Once CPU has been taken, it is permanent for that `WhisperLocal` instance: every
  later call goes straight to CPU and never touches CUDA again, even if the earlier failure was a
  fluke. There is no way to force a retry on CUDA short of restarting the process.

GPU on Windows needs cuBLAS + cuDNN 9 for CUDA 12 (install via `nvidia-cublas-cu12`,
`nvidia-cudnn-cu12` wheels, add their `bin` to PATH); CPU fallback is automatic if CUDA libs are
missing, per the paragraph above.

## 11. Audit and storage (SQLite)

Tables: `events` (one row per handled message, per spec §27), `sessions` (pending clarification, one per chat), `executions` (undo data, expires). See [DATA_MODEL.md](DATA_MODEL.md). Tokens never stored; LLM request context and raw response stored as JSON text.

Exactly one `events` row per handled message: opened at entry and closed once on every path, error
exits included. `events.executed = 1` means *the command ran*, not that something was written — a
`search` sets it and writes nothing (and records no `executions` row, since there is nothing to
undo). `events.error` normally holds the code the turn earned for itself; the one code that is
recorded as a fallback is `INBOX_FAILED` from `_expired_prefix`, because that rescue belongs to a
*previous* message and the row would otherwise close as a clean `EXECUTE` with no trace that the
rescued text was lost. A code the turn records for itself always wins over it.

`executions.reply_message_id` starts null — the row is written while the command runs, before its
reply exists — and the transport fills it in afterwards with `AuditStore.set_reply_message_id`, so
it can edit the Undo button away when the window closes.

## 12. Admin page

`app/admin/server.py`: a stdlib `http.server.ThreadingHTTPServer` on a daemon thread, exactly
three routes, no framework, no auth beyond the loopback guard below:

| Route | Does |
|---|---|
| `GET /` | Serves `page.html` (read once at import, from disk) — the tree of targets from the last snapshot, a description textarea and a required checkbox per field, and the inbox picker below. |
| `GET /api/targets` | JSON snapshot summary (`Discovery.last`) plus `inbox_locked` (`bool(settings.inbox_target_id)`) so the page knows whether to grey out its own inbox picker. |
| `POST /api/descriptions` | Validates the whole posted document against the last snapshot (unknown target/field id → 400, nothing written), merges it into `data/targets.yaml` (load → merge → save), invalidates the discovery cache, and returns how many targets actually changed. |

**Host loopback guard.** Every request — `GET` and `POST` alike — is rejected with `403
forbidden_host` before anything else runs unless its `Host` header names `127.0.0.1`, `localhost`
or `::1` (`_loopback_host`). This is a DNS-rebinding guard, not redundant with binding the socket
to `127.0.0.1`: binding keeps other *machines* out, but a page open in the user's own browser,
navigated to a hostile domain that resolves to `127.0.0.1` via DNS rebinding, would otherwise
still reach a server merely bound to loopback — the `Host` header is what a same-origin browser
request cannot forge to say anything else. `MAX_BODY_BYTES` (512 KiB) similarly bounds
`POST`'s `Content-Length` before `rfile.read()` ever runs, so a hostile/huge header cannot force
an unbounded blocking read.

**The inbox picker** is one radio button per target plus "инбокс не выбран", rendered from
`is_inbox` on each target in the `GET /api/targets` payload and saved as the `inbox` flag on
`POST /api/descriptions` (`notion/descriptions.py:TargetMeta.inbox`; at most one target may carry
it — a second `inbox: true` in one POST is rejected with 400). **It is disabled in the page itself
whenever `INBOX_TARGET_ID` is set** (`inbox_locked: true`, every radio's `disabled` attribute, and
a visible notice naming the env var): the `.env` override always wins over the yaml flag outright
(`notion/discovery.py`), so a click here would silently do nothing useful, and the page disables
the control rather than let the user believe it took effect. Unset `INBOX_TARGET_ID` to manage the
inbox target from this page instead (see `documentation/NOTION_SETUP.md`, "Inbox target").

Telegram `/refresh` forces re-discovery (invalidating the same cache this page invalidates on
save); `/targets` prints the tree as text, with the inbox target marked.

## 13. Model selection

Candidates fitting 8 GB VRAM alongside int8 Whisper turbo (~1.5 GB): `qwen3:8b` (default; benchmark winner after prompt tuning, `think: false`, see [BENCHMARK.md](BENCHMARK.md)), `llama3.1:8b` (runner-up), `qwen2.5:7b-instruct`, `gemma3:4b` (fast fallback). `mistral-nemo:12b` and `gemma3:12b` only with CPU offload. `tools/benchmark_llm.py` runs `tests/fixtures/ru_cases.yaml` against each and reports schema-validity rate, target/field accuracy, a safety-weighted `safe`/`wrong` split, and p50/p95 latency. `LLM_NUM_CTX=16384` default.

## 14. Security boundaries

- LLM receives: context JSON, user text, time. Never tokens, ids (only keys), URLs, tool lists.
- LLM output is data, never trusted as an address: `Candidate.target`/`.item`/`.fields` keys are
  plain strings at the Pydantic layer (the grammar Ollama is given constrains them, but nothing
  enforces that at the Python level), so `SemanticValidator` re-checks every one against the live
  context before anything is built into a `Command` — a foreign Notion id, a URL, or an
  unadvertised field key in the LLM's output is rejected there (`SEM_UNKNOWN_KEY`), never reaches
  `Executor`/`NotionProvider` (`tests/test_security.py` exercises this against the real
  orchestrator with fakes, not a mock that only proves "was called").
- Notion calls originate only from `direct.py`, invoked only by `executor.py` and `discovery.py`.
- Telegram allowlist enforced before any processing; unknown users get no reply, no audit row, and
  trigger no provider call at all (`app/telegram/auth.py`).
- Admin page bound to loopback, no auth beyond the `Host`-header guard (§12) — host-local by
  design.
- Logging never carries a token, at any level: besides `httpx`/`httpcore` (which log Telegram's
  token-bearing request URL at INFO), `telegram.ext.ExtBot` logs the same URL once at DEBUG from
  its own constructor — a leak the httpx floor alone does not cover, since it isn't an httpx
  request. `app/logging_setup.py` floors both to WARNING unconditionally. A third leak needs more
  than a floor: `telegram.ext`'s polling retry loop logs a token-bearing `InvalidToken` at ERROR
  *with* its traceback when Telegram rejects the token, so `configure()` also wraps the handler in
  a redacting formatter that rewrites every occurrence of the secret values `main()` explicitly
  hands it (the two tokens, never a `Settings`) to `***` — message, args and traceback alike. The
  same `InvalidToken` escaping `run_polling()` is caught in `main()` and reported as a config exit
  that names `TELEGRAM_BOT_TOKEN` and prints nothing of the exception. See its module docstring
  and `documentation/ERRORS.md`'s "Logging" section.

## 15. Releases and migrations

`pyproject.toml`'s `[project].version` is the version source (`app/version.py`); `release.py` bumps it, commits, tags, and builds `dist/notion_ai_bot-X.Y.Z.zip` (app files + `VERSION` + `manifest.json` with a lock hash). Production is a separate directory (e.g. `C:\apps\notion_ai_bot`) with its own `.env`/`.venv`/`data`, populated only from a release zip via `deploy/install.ps1` or `deploy/update.ps1` (`apply_update.py`), never `git clone`; `uv sync --frozen --no-dev` (re-run on updates only when the lock hash changed) provisions the venv. Schema migrations (`migrations/NNNN_name.sql`, journaled in `schema_migrations`) are applied only by the installer/updater or `tools/migrate.py` — never implicitly. At startup, `AuditStore.assert_schema_current()` verifies no migration is pending and refuses to run otherwise. See [RELEASE.md](../RELEASE.md) for the full process.
