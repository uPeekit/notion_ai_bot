# Implementation Plan

Waterfall phases, strictly ordered. Each task = code + tests, done before the next starts.
Complexity: S < 1 h, M 1–3 h, L 3–6 h, XL > 6 h (agent time).

## Open questions (do not block Phase 0–2)

| # | Question | Default if unanswered |
|---|---|---|
| Q1 | Do you want other fields than the title to be required (e.g. Приоритет)? | none; set later in `targets.yaml` |
| Q2 | Should the bot echo the transcription of every voice message? | yes |
| Q3 | Should `update` require a Confirm step instead of Undo? | Undo (auto-execute) |

## Phase 0 — Environment

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-001 | Install `uv`; `uv init` project, Python 3.12 pinned; `pyproject.toml` with deps: python-telegram-bot, httpx, pydantic, pydantic-settings, faster-whisper, pyyaml, pytest, pytest-asyncio, ruff | — | S | `uv sync` succeeds; `uv run pytest` runs 0 tests |
| T-002 | Install Ollama; pull `qwen3:8b`, `qwen2.5:7b-instruct`, `llama3.1:8b`, `gemma3:4b` | — | S | `ollama list` shows models; `/api/chat` with `format` returns JSON |
| T-003 | `.env.example`, `.gitignore` (data/, .env, .venv), README setup section: Notion integration creation + sharing pages, Telegram bot creation, CUDA libs for Whisper | T-001 | S | fresh clone can follow README to a running bot |
| T-004 | `app/config.py` Settings with all keys from DATA_MODEL §7; validation of allowlist and thresholds | T-001 | S | tests: missing token fails, defaults load |

## Phase 1 — Foundation

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-010 | `notion/snapshot.py` dataclasses; `interpretation/models.py` Pydantic models incl. discriminated `FieldValue` | T-004 | M | round-trip JSON tests; invalid status rejected |
| T-011 | `audit/store.py` SQLite schema + repository (events, sessions, executions), migrations | T-004 | M | tests on temp db: insert/read/expire |
| T-012 | `texts.py` Russian strings; `telegram/keyboards.py` builders | T-004 | S | snapshot tests of keyboards |
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
| T-040 | `conversation/session.py` + `resolver.py`: PendingSession persistence, apply button answers, expiry | T-031, T-011 | M | FSM tests for F2, F3, F7, F12, expiry |
| T-041 | `conversation/orchestrator.py`: full pipeline text → reply model (`Reply(text, keyboard, undo_id)`), free-text-while-pending handling (F4, F5), search reply formatting | T-040, T-034, T-022 | L | end-to-end tests with FakeLLM + FakeNotion for F1–F12, F14 |
| T-042 | `telegram/auth.py`, `telegram/handlers.py`: text, voice (download), callbacks, commands `/start /help /refresh /targets /undo /cancel`; wiring in `main.py` with startup checks | T-041 | L | handler tests with PTB test utilities; manual run |
| T-043 | `speech/base.py`, `speech/whisper_local.py`, lazy load, device auto-detect, CUDA→CPU fallback | T-004 | M | unit test with mocked model; manual test with a real voice note |
| T-044 | Voice path in orchestrator + transcript echo (F13) | T-042, T-043 | S | e2e test with fake STT |

## Phase 5 — Admin and polish

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-050 | `admin/server.py` + `page.html`: tree view, description/required editing, save → yaml + cache invalidation | T-014, T-015 | M | HTTP tests; manual browser check |
| T-051 | Audit completeness: every pipeline exit writes one `events` row with decision and durations | T-041 | S | tests assert row per flow |
| T-052 | Structured logging, `event_id` correlation, no-secrets check | T-042 | S | grep test on log output of a run with fake tokens |
| T-053 | README: usage, commands, tuning thresholds, remote Ollama/Whisper | T-042 | S | reviewed |

## Phase 6 — QA

| ID | Task | Depends | Cx | Acceptance |
|---|---|---|---|---|
| T-060 | Security tests: crafted LLM output with foreign ids/URLs/extra keys never reaches provider | T-041 | M | tests pass |
| T-061 | Integration tests (`-m integration`): real Ollama on fixtures; real Notion against a dedicated test page | T-041 | M | pass on host machine |
| T-062 | Live trial: 20 real voice commands, review audit rows, tune thresholds and prompt | T-061 | M | thresholds recorded in `.env.example` comments |

## Out of scope (MVP)

Delete/bulk operations, MCP provider, remote Whisper implementation (interface only), non-Russian UI, multi-user, cloud LLM fallback, page body structure in context.
