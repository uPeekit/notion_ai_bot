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

## 2. Runtime topology

Single `asyncio` process on the host machine:

```text
python-telegram-bot (long polling)
   │
   ├─ speech: faster-whisper in a worker thread (GPU if free, else CPU)
   ├─ llm: httpx → Ollama /api/chat  (OLLAMA_BASE_URL)
   ├─ notion: httpx → api.notion.com (NOTION_TOKEN never leaves this layer)
   ├─ sqlite: data/bot.sqlite  (audit, pending sessions, undo records)
   └─ admin: http.server thread on 127.0.0.1:8787 (targets.yaml editor)
```

No webhooks, no Docker, no public port.

## 3. Module layout

```text
notion_ai_bot/
├── app/
│   ├── config.py               Settings (pydantic-settings, .env)
│   ├── main.py                 wiring + startup checks
│   ├── texts.py                all Russian user-facing strings
│   ├── telegram/
│   │   ├── handlers.py         message/voice/callback handlers → Orchestrator
│   │   ├── keyboards.py        inline keyboard builders
│   │   └── auth.py             allowlist filter
│   ├── speech/
│   │   ├── base.py             SpeechToText protocol
│   │   ├── whisper_local.py    faster-whisper implementation
│   │   └── whisper_remote.py   HTTP client (post-MVP, stub interface only)
│   ├── llm/
│   │   ├── base.py             LLMClient protocol
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
│   │   └── mapper.py           Command → Notion payload
│   ├── validation/
│   │   ├── semantic.py         checks against snapshot
│   │   └── policy.py           thresholds → EXECUTE / CLARIFY / REJECT
│   ├── commands/
│   │   ├── models.py           CreateItem, UpdateItem, CreatePage, AppendBlocks, Search
│   │   └── executor.py         runs commands via provider, records undo info
│   ├── conversation/
│   │   ├── orchestrator.py     one entry point: handle(text|voice|callback)
│   │   ├── session.py          pending clarification FSM
│   │   └── resolver.py         applies user answers to interpretation
│   ├── audit/
│   │   └── store.py            SQLite schema + repository
│   └── admin/
│       ├── server.py           stdlib HTTP server thread
│       └── page.html           editor page
├── tools/
│   ├── benchmark_llm.py        run fixtures against N models, report accuracy/latency
│   └── discover.py             dump workspace snapshot to stdout
├── tests/
├── data/                        runtime files (gitignored): bot.sqlite, targets.yaml
├── documentation/
├── pyproject.toml
└── .env.example
```

## 4. Request pipeline

```text
Telegram update
  → auth (allowlist) → reject silently if not allowed
  → voice? download → SpeechToText → text (echo "🎤 …" to user)
  → Orchestrator.handle(chat_id, text)
      → session = pending for chat? (then this is a clarification answer, see §7)
      → snapshot = Discovery.get(ttl)                      (Notion)
      → ctx = ContextBuilder.build(snapshot, now, tz, session)
      → schema = OutputSchema.for_context(ctx)             (dynamic JSON schema)
      → raw = LLMClient.interpret(text, ctx, schema)        (Ollama, temp 0)
      → interp = Interpretation.model_validate(raw)        (Pydantic)
      → sem = SemanticValidator.check(interp, snapshot)
      → decision = Policy.evaluate(interp, sem)
      → EXECUTE: cmd = CommandBuilder.build(best, snapshot)
                 result = Executor.run(cmd)                 (Notion)
                 reply + Undo button; audit
        CLARIFY: session.save(question); ask with buttons; audit
        REJECT:  reply reason; audit
```

Callback (button) → `Resolver.apply(session, answer)` → back to `SemanticValidator` (no LLM call).
Free text while a session is pending → LLM call with `conversation_context` = pending interpretation + question + answer; result replaces the pending interpretation.

## 5. Workspace snapshot and discovery

`Discovery.refresh()`:

1. `POST /v1/search` paginated, no filter → all pages and data sources visible to the integration.
2. For each data source: `GET /v1/data_sources/{id}` → properties, title, description, parent database id.
3. For each data source: `PATCH /v1/data_sources/{id}/query` with `sorts=[last_edited_time desc]`, `page_size=ITEMS_PER_TARGET` → items (id, title, status/checkbox if present).
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
  "operations": ["create", "update", "append", "search"],
  "targets": [
    {"key": "t1", "kind": "database", "name": "Покупки", "path": "Дом / Покупки",
     "description": "Список покупок…",
     "fields": [
       {"key": "t1.f1", "name": "Название", "type": "title", "required": true},
       {"key": "t1.f2", "name": "Магазин", "type": "select", "options": {"t1.f2.o1": "Rimi", "t1.f2.o2": "Prisma"}},
       {"key": "t1.f3", "name": "Куплено", "type": "checkbox"}
     ],
     "items": {"t1.i1": "Хлеб", "t1.i2": "Молоко (куплено)"}},
    {"key": "t2", "kind": "page", "name": "Идеи", "path": "Идеи", "description": "…",
     "children": {"t2.i1": "Отпуск 2027"}}
  ]
}
```

`required` for a database = the `title` property only, unless `targets.yaml` marks other fields required (semantic metadata). Keys are regenerated per request; the mapping key → Notion id is held by the app.

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
  item_candidates: [enum] | null                  when several items plausible
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

### Prompt

System prompt (Russian): role = classifier and extractor, not an agent. Rules: pick only from provided keys; return every writable field with a status; never guess a select option; dates resolved from `now`; return alternatives only when plausible; for `update`/`append`, `item` must come from `items`; thinking disabled (`think: false` for qwen3).

## 7. Conversation state

```text
IDLE ──text──▶ INTERPRETING ──EXECUTE──▶ IDLE (+undo record)
                    │
                    ├──REJECT──▶ IDLE
                    │
                    └──CLARIFY──▶ WAITING (session saved)
                                    │
                        button ─────┤─▶ Resolver.apply → validate → EXECUTE|CLARIFY|REJECT
                        free text ──┤─▶ LLM with session context → validate → …
                        timeout ────┘─▶ IDLE (session expired message)
```

One pending session per chat. A new unrelated message while WAITING: the LLM is told about the pending question; if its answer keeps the same target and resolves the question, continue; otherwise the old session is dropped and the new text is handled fresh.

Clarification question types (each has a button layout in `keyboards.py`):

| Type | Trigger | Buttons |
|---|---|---|
| `target` | target margin < threshold | one per candidate target + Отмена |
| `item` | `item_candidates` set or item missing for update/append | one per candidate item (max 8) + Отмена |
| `field_required` | required field `not_mentioned` | options for select/status/relation; free-text prompt for others |
| `field_ambiguous` | field status `ambiguous` | one per candidate value |
| `date` | date confidence < `POLICY_DATE_MIN` | proposed date ✓ / other date (free text) |

One question per message. Several open issues are asked in order: target → item → required fields → ambiguous fields → low-confidence date.

## 8. Policy engine

Deterministic, thresholds from settings:

```text
REJECT if intent = unknown
      or target not in snapshot, or field not writable, or value type invalid
      or (update|append) and item not in target's items after clarification
CLARIFY if intent.confidence < POLICY_INTENT_MIN
      or best.confidence < POLICY_TARGET_MIN
      or (best.confidence - second.confidence) < POLICY_TARGET_MARGIN   (only when second exists)
      or any required field not_mentioned / ambiguous
      or any provided field confidence < POLICY_FIELD_MIN
      or any date value confidence < POLICY_DATE_MIN
      or update|append with item null and item_candidates non-empty
EXECUTE otherwise
```

Risk classes: `create`, `append`, `search` = LOW; `update` = MEDIUM. Both auto-execute in MVP. Undo available for `create` (archive page), `update` (restore previous property values), `append` (delete appended blocks). Undo button expires after `UNDO_WINDOW_SECONDS`.

## 9. Commands and Notion mapping

| Command | Fields | Notion call |
|---|---|---|
| `CreateItem` | data_source_id, properties | `POST /v1/pages` parent `data_source_id` |
| `UpdateItem` | page_id, properties | `PATCH /v1/pages/{id}` |
| `CreatePage` | parent_page_id, title, body | `POST /v1/pages` parent `page_id` + paragraph blocks |
| `AppendBlocks` | page_id, paragraphs | `PATCH /v1/blocks/{id}/children` |
| `Search` | data_source_id or None, query | `PATCH /v1/data_sources/{id}/query` title filter, or `POST /v1/search` |
| `ArchivePage` (undo) | page_id | `PATCH /v1/pages/{id}` `archived: true` |
| `DeleteBlocks` (undo) | block_ids | `DELETE /v1/blocks/{id}` |

Commands hold Notion ids resolved by the app from keys. `mapper.py` is the only place that builds Notion JSON. LLM output never reaches it.

## 10. Speech

faster-whisper, model `WHISPER_MODEL` (default `large-v3-turbo`), `compute_type=int8`, `device=auto` (CUDA if available, else CPU), `language=ru` hint (configurable), `vad_filter=True`. Runs via `asyncio.to_thread`. Model loaded lazily on first voice message and kept. Transcript echoed to the user and stored in audit. GPU on Windows needs cuBLAS + cuDNN 9 for CUDA 12 (install via `nvidia-cublas-cu12`, `nvidia-cudnn-cu12` wheels, add their `bin` to PATH); CPU fallback is automatic if CUDA libs are missing.

## 11. Audit and storage (SQLite)

Tables: `events` (one row per handled message, per spec §27), `sessions` (pending clarification, one per chat), `executions` (undo data, expires), `settings` (last successful discovery hash). See [DATA_MODEL.md](DATA_MODEL.md). Tokens never stored; LLM request context and raw response stored as JSON text.

## 12. Admin page

`GET /` → HTML tree of targets (from last snapshot) with a textarea per target. `GET /api/targets` → JSON snapshot summary + descriptions. `POST /api/descriptions` → writes `data/targets.yaml` and invalidates the snapshot cache. Bound to `127.0.0.1` only. Telegram `/refresh` forces re-discovery, `/targets` prints the tree as text.

## 13. Model selection

Candidates fitting 8 GB VRAM alongside int8 Whisper turbo (~1.5 GB): `qwen3:8b` (default, `think: false`), `qwen2.5:7b-instruct`, `llama3.1:8b`, `gemma3:4b` (fast fallback). `mistral-nemo:12b` and `gemma3:12b` only with CPU offload. `tools/benchmark_llm.py` runs `tests/fixtures/ru_cases.yaml` against each and reports schema-validity rate, target accuracy, field accuracy, p50/p95 latency. `LLM_NUM_CTX=16384` default.

## 14. Security boundaries

- LLM receives: context JSON, user text, time. Never tokens, ids (only keys), URLs, tool lists.
- LLM output is data; the only consumers are Pydantic validation and the semantic validator.
- Notion calls originate only from `direct.py`, invoked only by `executor.py` and `discovery.py`.
- Telegram allowlist enforced before any processing; unknown users get no reply.
- Admin page bound to loopback, no auth (host-local by design).
