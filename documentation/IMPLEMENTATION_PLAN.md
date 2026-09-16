# Implementation Plan

Waterfall phases, strictly ordered. Each task = code + tests, done before the next starts.
Complexity: S < 1 h, M 1–3 h, L 3–6 h, XL > 6 h (agent time).

## Resolved questions

| # | Question | Answer |
|---|---|---|
| Q1 | Required fields beyond title? | Set per table in `targets.yaml` via admin page; default title only |
| Q2 | Echo voice transcription? | No. Audit only. Reply shows the actual Notion change |
| Q3 | Confirm before update? | No. Auto-execute with Undo |
| Q4 | Telegram / Notion tokens? | Not yet; README setup steps, needed from Phase 4 |

## Phase 0 — Environment

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-001 | Install `uv`; `uv init` project, Python 3.12 pinned; `pyproject.toml` with deps: python-telegram-bot, httpx, pydantic, pydantic-settings, faster-whisper, pyyaml, pytest, pytest-asyncio, ruff | — | S | `uv sync` succeeds; `uv run pytest` runs 0 tests |
| T-002 | Install Ollama; pull `mistral-nemo:12b` (default), `qwen3:8b`, `qwen2.5:7b-instruct`, `llama3.1:8b`, `gemma3:4b` | — | S | `ollama list` shows models; `/api/chat` with `format` returns JSON |
| T-003 | `.env.example`, `.gitignore` (data/, .env, .venv), README setup section: Notion integration creation + sharing pages, Telegram bot creation, CUDA libs for Whisper | T-001 | S | fresh clone can follow README to a running bot |
| T-004 | `app/config.py` Settings with all keys from DATA_MODEL §7; validation of allowlist and thresholds | T-001 | S | tests: missing token fails, defaults load |

## Phase 1 — Foundation

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-010 | `notion/snapshot.py` dataclasses; `interpretation/models.py` Pydantic models incl. discriminated `FieldValue` | T-004 | M | round-trip JSON tests; invalid status rejected |
| T-011 | `audit/store.py` SQLite schema + repository (events, sessions, executions), migrations | T-004 | M | tests on temp db: insert/read/expire |
| T-012 | `texts.py` Russian strings; `telegram/keyboards.py` builders **(`texts.py` done, Plan 3a Task 1 — every question/execution/inbox/error template; `telegram/keyboards.py` itself deferred to Plan 3b, its job done for now by `conversation/reply.py`'s transport-neutral `Button`/`Reply`)** | T-004 | S | snapshot tests of keyboards |
| T-013 | `notion/provider.py` protocol + `notion/direct.py` httpx client: search, get data source, query, create page, update page, append blocks, delete block, users/me; retries on 429/5xx | T-004 | L | tests with `httpx.MockTransport` for each call and retry path |
| T-014 | `notion/descriptions.py` targets.yaml read/merge/write | T-010 | S | tests: new ids added, existing descriptions preserved |
| T-015 | `notion/discovery.py` → `WorkspaceSnapshot` (pages tree, data sources, items, relation options, descriptions) with TTL cache | T-013, T-014 | L | tests with fixture JSON of a fake workspace; `tools/discover.py` prints tree against real Notion |

## Phase 2 — Interpretation

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-020 | `llm/context.py` ContextBuilder: key assignment, compact JSON, time context, pending-session context | T-015 | M | golden tests; key map round-trips; size test for 10 targets × 50 items < 6k tokens (approx by chars) |
| T-021 | `llm/output_schema.py` dynamic JSON schema with enums per context; Pydantic validation of response into `Interpretation` | T-020 | L | schema valid per Draft-07; enum sets match context; fixture responses validate |
| T-022 | `llm/base.py` protocol; `llm/ollama.py` client (`/api/chat`, `format`, `think:false`, temp 0, timeout, retry-once on invalid JSON) | T-021 | M | tests with mocked httpx; error mapping |
| T-023 | `llm/prompts.py` Russian system prompt + 3 few-shot examples | T-021 | M | manual smoke via `tools/benchmark_llm.py` |
| T-024 | `tests/fixtures/ru_cases.yaml` (≥ 40 cases: classification, ambiguity, optional fields, missing required, invalid values, temporal, update, append, search) + `tools/benchmark_llm.py` | T-022, T-023 | L | report table per model: validity %, target acc, field acc, p50/p95 ms |
| T-025 | Run benchmark on this machine; pick default `LLM_MODEL`; record results in `documentation/BENCHMARK.md` | T-024, T-002 | M | doc with numbers and chosen model |

## Phase 3 — Validation and policy

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-030 | `validation/semantic.py`: key existence, writability, type checks, date parsing, option membership, operation support | T-021 | M | table-driven tests incl. every ERRORS.md `SEM_*` code |
| T-031 | `validation/policy.py`: thresholds → decision + ordered list of `Question`s | T-030 | M | boundary tests 0.89/0.90, margin 0.09/0.10, date 0.79/0.80; required missing → CLARIFY |
| T-032 | `commands/models.py` + `CommandBuilder` (candidate + resolution + snapshot → Command with Notion ids) | T-030 | M | tests: every field type maps; unknown key impossible |
| T-033 | `notion/mapper.py` Command → Notion JSON | T-032 | M | golden payload tests per property type and per command |
| T-034 | `commands/executor.py`: run command, build undo record, undo execution | T-033, T-013, T-011 | M | tests with fake provider: create→archive, update→restore, append→delete blocks |

## Phase 4 — Conversation and Telegram

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-040 | `conversation/session.py` + `resolver.py`: PendingSession persistence, apply button answers, expiry **(done, Plan 3a Tasks 2–3)** | T-031, T-011 | M | FSM tests for F2, F3, F7, F12, expiry |
| T-041 | `conversation/orchestrator.py`: full pipeline text → reply model (`Reply(text, keyboard, undo_id)`), free-text-while-pending handling (F4, F5), search reply formatting **(done, Plan 3a Task 5 — `handle_text`/`handle_callback`/`undo`/`cancel`/`flush_expired_sessions`)** | T-040, T-034, T-022 | L | end-to-end tests with FakeLLM + FakeNotion for F1–F12, F14 |
| T-042 | `telegram/auth.py`, `telegram/handlers.py`: text, voice (download), callbacks, commands `/start /help /refresh /targets /undo /cancel`; wiring in `main.py` with startup checks **(done, Plan 3b Tasks 1/2/4 — `app/telegram/auth.py`, `app/telegram/handlers.py`, `app/main.py`)** | T-041 | L | handler tests with PTB test utilities; manual run |
| T-043 | `speech/base.py`, `speech/whisper_local.py`, lazy load, device auto-detect, CUDA→CPU fallback **(done, Plan 3b Task 3)** | T-004 | M | unit test with mocked model; manual test with a real voice note |
| T-044 | Voice path in orchestrator, transcript to audit (F13) **(done, Plan 3b Task 2 — `_on_voice` in `app/telegram/handlers.py`)** | T-042, T-043 | S | e2e test with fake STT |

**Scope addition (Plan 3a):** the inbox fallback was not in this plan's original task list — `conversation/inbox.py`, `INBOX_MODE`/`INBOX_TARGET_ID` in `config.py`, `TargetMeta.inbox`/`Target.is_inbox` and `Discovery._resolve_inbox`, and `AuditStore.expired_sessions`/`pop_session`/`pop_expired_session`. Added alongside T-040/T-041 so a message the pipeline can't resolve is appended to a user-flagged target instead of being dropped. See ARCHITECTURE.md §1/§4/§7, DATA_MODEL.md §2/§7, NOTION_SETUP.md "Inbox target".

## Phase 5 — Admin and polish

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-050 | `admin/server.py` + `page.html`: tree view, description/required editing, save → yaml + cache invalidation **(done, Plan 3b Task 5 — three routes, the `Host`-loopback guard, and the inbox radio picker; ARCHITECTURE.md §12)** | T-014, T-015 | M | HTTP tests; manual browser check |
| T-051 | Audit completeness: every pipeline exit writes one `events` row with decision and durations **(done — orchestrator side from Plan 3a Task 5, `_turn`/`_open`/`_finish` guarantee exactly one closed row per handled message on every exit, error paths included; the Telegram-layer `reply_message_id` wiring completed in Plan 3b Task 2, `handlers.py:_send` → `AuditStore.set_reply_message_id`)** | T-041 | S | tests assert row per flow |
| T-052 | Structured logging, `event_id` correlation, no-secrets check **(done, Plan 3b Task 4 — `app/logging_setup.py`; extended in Task 6 to also floor `telegram.ext.ExtBot`, a second token-bearing DEBUG log the original httpx/httpcore floor did not cover)** | T-042 | S | grep test on log output of a run with fake tokens |
| T-053 | README: usage, commands, tuning thresholds, remote Ollama/Whisper **(done, Plan 3b Task 6)** | T-042 | S | reviewed |

## Phase 6 — QA

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-060 | Security tests: crafted LLM output with foreign ids/URLs/extra keys never reaches provider **(done, Plan 3b Task 6 — `tests/test_security.py`: foreign id/URL/extra-key candidates against the real Orchestrator+SemanticValidator; LLM context scanned for ids/URLs/tokens; replies, audit rows and log output scanned for both tokens; unauthorised user produces no audit row and no provider call)** | T-041 | M | tests pass |
| T-061 | Integration tests (`-m integration`): real Ollama on fixtures; real Notion against a dedicated test page | T-041 | M | pass on host machine |
| T-062 | Live trial: 20 real voice commands, review audit rows, tune thresholds and prompt | T-061 | M | thresholds recorded in `.env.example` comments |

## Out of scope (MVP)

Delete/bulk operations, MCP provider, remote Whisper (no `whisper_remote.py` exists yet, not even
a stub — `WHISPER_DEVICE=cpu` is the only way today to run transcription without a GPU, not a way
to move it to another machine; see ARCHITECTURE.md §3), non-Russian UI, multi-user, cloud LLM
fallback, page body structure in context.
