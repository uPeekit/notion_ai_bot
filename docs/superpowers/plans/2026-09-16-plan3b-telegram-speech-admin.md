# Plan 3b: Telegram, Speech, Startup, Admin Page — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the finished pipeline into a bot you can actually talk to: a Telegram front end over `Orchestrator`, Russian voice input through local Whisper, a `main.py` that wires everything with startup checks, and the local admin page that edits target descriptions, required-field flags and the inbox choice.

**Architecture:** Plan 3a ended at `Reply(text, buttons, undo_id)` — a transport-neutral value. This plan adds the transport and nothing else: `telegram/keyboards.py` renders a `Reply` into `InlineKeyboardMarkup`, `telegram/auth.py` gates every update on the user-id allowlist, `telegram/handlers.py` maps Telegram updates onto the four orchestrator entry points, `speech/` transcribes a voice note to text before `handle_text`, `admin/` serves the loopback editor, and `main.py` builds every object once, runs the startup checks from `documentation/ERRORS.md`, schedules the session sweeper, and starts polling. No decision logic moves here: if a handler needs to decide something about Notion, that is a bug.

**Tech Stack:** Python 3.12, python-telegram-bot 22.8 (already a dependency), faster-whisper 1.2, stdlib `http.server` in a thread, existing `Orchestrator`, `Discovery`, `AuditStore`, `Settings`.

**Spec:** `documentation/ARCHITECTURE.md` §2, §3, §4, §7, §10, §12, §14; `documentation/ERRORS.md` (startup checks, `AUTH_DENIED`, `STT_*`, logging rules); `documentation/FLOWS.md` F13 (voice failure), F15 (admin), F16 (unauthorized); `documentation/DATA_MODEL.md` §7; IMPLEMENTATION_PLAN T-042, T-043, T-044, T-050, T-052, T-053, T-060, T-062.

## Global Constraints

- **The transport decides nothing.** Handlers parse a Telegram update, call one orchestrator method, render the `Reply`, and stop. No Notion call, no LLM call, no policy, no session handling outside what the orchestrator exposes. A handler that builds a Notion payload or inspects an `Interpretation` is a defect.
- **The allowlist gates everything.** A user not in `TELEGRAM_ALLOWED_USER_IDS` gets no reply at all (not an error message): log `AUTH_DENIED` at WARNING with the user id, and drop the update. This is checked before the message text is read, before transcription, and before any audit row is opened.
- **No secrets anywhere.** The bot token and Notion token never reach a log line, a reply, an audit row, or the admin page. `main.py` logs configuration by key name and value *shape* only.
- **Logging carries `event_id`.** Every log record emitted while handling an update carries the audit `event_id` (a `contextvars`-backed filter, set by the handler once the orchestrator opens the row). Message text is never logged above DEBUG.
- **The orchestrator already serialises per chat** (`_chat_lock`); the transport must not add its own locking, queueing, or `drop_pending_updates`-style ordering assumptions.
- **Whisper loads lazily** on the first voice message and stays loaded; it runs in a worker thread (`asyncio.to_thread`) so polling is never blocked. A CUDA failure falls back to CPU **for the process lifetime** and logs it once, not per message.
- **The admin page binds to `127.0.0.1` only**, has no authentication by design, and serves one embedded HTML page plus two JSON endpoints. It never displays or accepts a token. `ADMIN_UI_PORT=0` disables it.
- Anything user-facing is Russian and lives in `app/texts.py` — the Cyrillic guard test (`tests/test_reply.py`) covers `app/**` and will fail the commit otherwise. `admin/page.html` is exempt (it is not a Python module); its Russian lives in the template.
- Tests must not touch the network, Telegram, Ollama, Notion or a real Whisper model. python-telegram-bot objects are constructed offline; the orchestrator is a fake in handler tests and real only in `main.py` wiring tests.
- `uv run ruff check .` (line length 100) and `uv run pytest -q` pass before each commit; the existing 427 tests must keep passing. Named `git add` only. Commit trailer on its own line after a blank line: `Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>`

## File structure

```text
app/telegram/__init__.py
app/telegram/auth.py           allowlist filter + AUTH_DENIED logging
app/telegram/keyboards.py      Reply -> InlineKeyboardMarkup; callback data pass-through
app/telegram/handlers.py       text / voice / callback / commands -> Orchestrator
app/speech/__init__.py
app/speech/base.py             SpeechToText protocol, SpeechError/SpeechEmpty
app/speech/whisper_local.py    faster-whisper, lazy load, CUDA->CPU fallback
app/admin/__init__.py
app/admin/server.py            stdlib HTTP server thread, GET / and the two JSON endpoints
app/admin/page.html            embedded editor page
app/logging_setup.py           structured logging + event_id contextvar filter
app/main.py                    wiring, startup checks, sweeper task, polling
tests/test_auth.py, tests/test_keyboards.py, tests/test_handlers.py, tests/test_speech.py,
tests/test_admin.py, tests/test_main.py, tests/test_logging.py
```

---

### Task 1: Auth and keyboards

**Files:**
- Create: `app/telegram/__init__.py` (empty), `app/telegram/auth.py`, `app/telegram/keyboards.py`, `tests/test_auth.py`, `tests/test_keyboards.py`

**Interfaces:**
- `is_allowed(user_id: int | None, allowed: frozenset[int]) -> bool` — `None` (channel posts, edited service messages) is never allowed.
- `deny(user_id: int | None, chat_id: int | None) -> None` — logs `AUTH_DENIED` at WARNING with the ids and nothing else.
- `allowed_filter(settings: Settings) -> filters.BaseFilter` — a python-telegram-bot filter built from `Settings.allowed_user_ids`, so unauthorised updates never reach a handler callback.
- `to_markup(reply: Reply) -> InlineKeyboardMarkup | None` — `None` when the reply has no buttons; one `InlineKeyboardButton(label, callback_data=id)` per `Button`, preserving row structure. Assert the invariant the orchestrator already guarantees: `len(id.encode()) <= 64`; a longer id raises rather than silently truncating (it would be a programming error upstream, not user input).

- [ ] **Step 1: tests**
  - `tests/test_auth.py`: allowed id passes; unknown id denied; `None` denied; empty allowlist denies everyone; `deny` logs at WARNING and the record contains neither text nor a token.
  - `tests/test_keyboards.py`: a `Reply` with two option rows plus a trailing row round-trips to the same shape; callback data is byte-identical to `Button.id`; no buttons → `None`; a 65-byte id raises.
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: Telegram allowlist and keyboard rendering`.

---

### Task 2: Handlers

**Files:**
- Create: `app/telegram/handlers.py`, `tests/test_handlers.py`

**Interfaces:**
- `register(app: Application, orch: Orchestrator, settings: Settings, speech: SpeechToText) -> None` — installs every handler on the PTB `Application`, all gated by `allowed_filter`.
- Handlers, each of which awaits exactly one orchestrator call and then `_send(update, reply)`:
  - text message → `orch.handle_text(chat_id, user_id, text)`
  - voice / audio / video note → download to a temp file, `speech.transcribe(path)`, then `orch.handle_text(chat_id, user_id, transcript, kind="voice", transcript=transcript)`; `SpeechEmpty` → `texts.ERRORS["STT_EMPTY"]`, `SpeechError` → `texts.ERRORS["STT_FAILED"]`; the temp file is deleted in a `finally`.
  - callback query → `answer()` the query immediately (Telegram's 10-second budget), then `orch.handle_callback(chat_id, user_id, data)`.
  - `/start`, `/help` → static Russian help from `texts.py` listing the commands and how the inbox works.
  - `/undo` → `orch.undo(chat_id)`; `/cancel` → `orch.cancel(chat_id)`.
  - `/refresh` → `discovery.invalidate()` + a re-discovery, replying with the target count; `/targets` → the target tree as text (name, path, kind, `(разное)` marker on the inbox), from the last snapshot.
- `_send(update, reply)` — sends `reply.text` with `to_markup(reply)`, no `parse_mode` (Plan 3a's text is plain and may contain `«»`, `—`, emoji). When `reply.undo_id` is set, record the sent message id via `AuditStore.set_reply_message_id(reply.undo_id, message_id)` so a later turn can edit the message.
- `error_handler(update, context)` — PTB's global error hook: log the exception with `event_id` if one is set, reply `texts.ERRORS["INTERNAL"]` when a chat is known, and never re-raise.

**Notes for the implementer:**
- The orchestrator never raises (Plan 3a invariant), so a handler exception means a transport bug — do not paper over it with a broad `except` inside a handler; that is what `error_handler` is for.
- A voice note arrives as `update.message.voice` (OGG/Opus). Use `await file.download_to_drive(tmp_path)`; faster-whisper reads OGG via its bundled decoder, so no ffmpeg dependency is introduced.
- `/refresh` and `/targets` are the only handlers allowed to touch `Discovery` directly, and only for the two calls named above.

- [ ] **Step 1: tests** (`tests/test_handlers.py`, with a `FakeOrchestrator` recording calls and returning canned `Reply`s, a `FakeSpeech`, and PTB `Update`/`Message` objects built offline):
  - a text message reaches `handle_text` with the right chat/user/text and the reply is sent with the right markup;
  - a voice note transcribes, calls `handle_text` with `kind="voice"` and the transcript in both fields, and deletes the temp file (assert the path is gone);
  - `SpeechEmpty` and `SpeechError` each produce their own message and never call the orchestrator;
  - a callback query is answered before the orchestrator is called (assert call order), then dispatched;
  - `/undo`, `/cancel`, `/start`, `/help`, `/refresh`, `/targets` each hit their target and reply;
  - an unauthorised user id produces **no** send at all for text, voice and callback;
  - a reply carrying `undo_id` triggers `set_reply_message_id` with the sent message id;
  - `error_handler` replies once and swallows the exception.
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: Telegram handlers over the orchestrator`.

---

### Task 3: Speech

**Files:**
- Create: `app/speech/__init__.py` (empty), `app/speech/base.py`, `app/speech/whisper_local.py`, `tests/test_speech.py`

**Interfaces:**
- `class SpeechToText(Protocol): async def transcribe(self, path: Path) -> str: ...`
- `SpeechError(Exception)`; `SpeechEmpty(SpeechError)` — raised when the decoder returns no segments or only whitespace.
- `class WhisperLocal(SpeechToText)`: `__init__(settings: Settings)`; lazy `_load()` on first use inside the worker thread; `transcribe` runs the model under `asyncio.to_thread`, joins the segment texts, strips, and raises `SpeechEmpty` on an empty result. Model `WHISPER_MODEL`, `compute_type=WHISPER_COMPUTE_TYPE`, `device=WHISPER_DEVICE` (`auto` → try CUDA, fall back to CPU), `language=WHISPER_LANGUAGE`, `vad_filter=True`.
- CUDA failure handling: any exception during a CUDA load or the first CUDA transcription switches the instance to CPU **once**, logs one WARNING naming the original error, and retries; a second failure raises `SpeechError`. The fallback is remembered for the process lifetime — never re-attempt CUDA per message.

- [ ] **Step 1: tests** (a fake model class injected through a seam — do **not** import faster-whisper in tests):
  - a two-segment result joins with a single space and strips;
  - no segments / whitespace-only → `SpeechEmpty`;
  - the model is constructed once across two transcriptions (lazy + cached);
  - a CUDA load error falls back to CPU, logs once, and succeeds; a second CUDA-then-CPU failure raises `SpeechError`; the fallback is not re-attempted on the next call (assert the constructor saw `cpu` the second time);
  - `transcribe` does not block the event loop (assert it is awaited off-thread via the injected seam's recorded thread name, or that `asyncio.to_thread` was used).
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: local Whisper speech-to-text with CPU fallback`.

---

### Task 4: Logging, wiring and startup checks

**Files:**
- Create: `app/logging_setup.py`, `app/main.py`, `tests/test_logging.py`, `tests/test_main.py`
- Modify: `.env.example` if a new key appears (justify it), `documentation/ERRORS.md` if a check changes

**Interfaces:**
- `logging_setup.configure(level: str) -> None` — root handler with `%(asctime)s %(levelname)s %(name)s [event=%(event_id)s] %(message)s`; a filter injecting the `event_id` contextvar (`-` when unset).
- `logging_setup.event_id_var: ContextVar[str]` plus `bind_event(event_id: int | None)` used by the handlers.
- `main.build(settings) -> App` — a small dataclass holding every constructed object (store, discovery, orchestrator, speech, admin server, PTB application), so tests can build without polling.
- `main.startup_checks(app) -> None` — in the order `documentation/ERRORS.md` fixes: settings loaded (`Settings.require_telegram()`, exit 2 on failure); `AuditStore.assert_schema_current()` (exit 4 with the "run the updater" hint — migrations are applied by the installer/updater, never implicitly); Ollama `GET /api/tags` (WARNING when `LLM_MODEL` is absent from the list, not fatal); Notion `GET /v1/users/me` (exit 3 on 401); initial discovery writing `targets.yaml` and logging the target count plus which target is the inbox (or a WARNING that none is flagged and the fallback is inert); Whisper deliberately not loaded; admin server started when `ADMIN_UI_PORT > 0`; polling last.
- `main.main()` — `asyncio` entry point: configure logging, build, run the checks, start the session sweeper as a periodic task (`flush_expired_sessions` every `SESSION_TTL_S / 3`, never overlapping itself, exceptions logged and swallowed), then `Application.run_polling()`. A clean shutdown closes the store, stops the sweeper and the admin server.
- Exit codes: 2 config, 3 Notion auth, 4 pending migration. Each prints one Russian-free operator line to stderr (this is host-side, not user-facing) and the remedy.

- [ ] **Step 1: tests**
  - `tests/test_logging.py`: a record emitted inside `bind_event(7)` carries `event=7`; outside it carries `event=-`; a record containing a fake token value is *not* produced by `configure` itself (assert the formatter has no access to settings).
  - `tests/test_main.py` (all fakes, no network): `startup_checks` exits 2 when Telegram settings are missing, 3 on a Notion 401, 4 when a migration is pending; a missing Ollama model warns but continues; a successful run logs the target count and names the inbox target; with no inbox flagged it logs the "fallback inert" warning; the sweeper task runs at least twice against a fake clock and swallows an exception from one run; `ADMIN_UI_PORT=0` starts no server.
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: application wiring, startup checks and structured logging`.

---

### Task 5: Admin page

**Files:**
- Create: `app/admin/__init__.py` (empty), `app/admin/server.py`, `app/admin/page.html`, `tests/test_admin.py`

**Interfaces:**
- `class AdminServer`: `__init__(settings, discovery, descriptions)`, `start()` (daemon thread, `127.0.0.1` only), `stop()`, `port` property.
- `GET /` → `page.html` (read once at import, served as UTF-8 HTML).
- `GET /api/targets` → `{"fetched_at": iso, "targets": [{"id", "kind", "name", "path", "description", "is_inbox", "fields": [{"id", "name", "type", "required", "description"}]}]}` — built from the **last** snapshot (never triggers a Notion fetch; when there is no snapshot yet, `{"targets": []}` and the page says so).
- `POST /api/descriptions` → body `{"targets": {"<id>": {"description": str, "inbox": bool, "fields": {"<field_id>": {"description": str, "required": bool}}}}}` → merges into `data/targets.yaml` through `Descriptions.save`, invalidates the discovery cache, returns `{"saved": n}`. At most one target may carry `inbox: true`; a body with two is rejected `400`.
- The page: a tree of targets by `path`, a textarea per target description, per-field `required` checkbox and description input, one radio group across all targets choosing the inbox (plus a "none" option), and a Save button posting the whole document. Russian labels. No frameworks, no CDN, one inline `<style>` and one inline `<script>`.
- Security: bind `127.0.0.1`; reject any request whose `Host` header is not loopback (DNS-rebinding guard); no directory listing; no path traversal (only the three routes exist); nothing about tokens is ever rendered or accepted.

- [ ] **Step 1: tests** (`http.client` against the real server on port 0, a fake `Discovery` holding a `sample_snapshot()`):
  - `GET /` returns 200 HTML containing the page title;
  - `GET /api/targets` mirrors the snapshot, including `is_inbox` and per-field `required`;
  - with no snapshot, `targets` is empty and the status is still 200;
  - `POST /api/descriptions` writes the yaml (assert the file content), invalidates the cache (assert the flag), and preserves untouched targets' existing descriptions;
  - two inbox flags → 400 and the yaml is unchanged;
  - a non-loopback `Host` header → 403;
  - an unknown path → 404;
  - malformed JSON → 400, no write.
- [ ] **Step 2: implement.**
- [ ] **Step 3:** ruff + pytest; commit `feat: local admin page for descriptions, required fields and the inbox`.

---

### Task 6: README, QA and documentation

**Files:** `README.md`, `documentation/*`, `tests/test_security.py`

- [ ] **Step 1: security tests** (`tests/test_security.py`, wiring the real orchestrator with fakes):
  - a crafted `Interpretation` naming a foreign Notion id, a URL, or an extra key never reaches the provider (assert every provider call's payload);
  - a reply for any flow contains neither token value;
  - the LLM context payload for a request contains no Notion id, no URL and no token (scan the recorded context);
  - a log capture over one full flow (text → execute → undo) contains neither token, and no message text above DEBUG;
  - an unauthorised Telegram user produces no audit row and no provider call.
- [ ] **Step 2: README** — what the bot is, install (`deploy/install.ps1`), the three tokens and where to get each (link `documentation/NOTION_SETUP.md`, @BotFather for Telegram, `ollama pull qwen3:8b`), the `.env` keys that matter day to day, how to flag the inbox page, the Telegram commands, how to read the audit log, how to tune the policy thresholds, and how to point `OLLAMA_BASE_URL`/`WHISPER_*` at another machine later. Keep it operator-focused; design lives in `documentation/`.
- [ ] **Step 3: documentation sync** — ARCHITECTURE §2/§3/§10/§12 to match what was built; ERRORS.md startup-check list and exit codes to match `main.py`; FLOWS F13/F15/F16 to match the real strings; IMPLEMENTATION_PLAN T-042…T-053 marked done.
- [ ] **Step 4:** ruff + pytest; commit `docs: operator README and Plan 3b documentation sync`.

---

## Verification

- `uv run pytest -q` green; `uv run ruff check .` clean.
- No Cyrillic outside `app/texts.py`, `app/llm/prompts.py`, `app/llm/context.py` (existing guard test; `admin/page.html` is not a Python module and is exempt).
- No test performs network I/O or loads a real Whisper model.
- An unauthorised user produces no reply, no audit row and no provider call (test enforced).
- Neither token appears in any reply, log record or LLM context (test enforced).

## Manual acceptance (user, after merge)

Not automatable here; the plan is done when these are possible, and the user runs them on a released build:

1. `ollama pull qwen3:8b`, create a bot with @BotFather, put both tokens plus your Telegram user id in `.env`.
2. Flag a page as the inbox on `http://127.0.0.1:8787`.
3. Text: «купи молоко» → a row appears in the shopping list, the reply names it, Undo works.
4. Voice: the same sentence as a voice note.
5. A deliberately vague message → a question with buttons, and «В разное» files it in the inbox.

## Out of scope

Remote Whisper (`whisper_remote.py` interface only), MCP provider, non-Russian UI, multi-user, delete/bulk operations, the live-trial threshold tuning (T-062, needs real usage), and cutting release 0.1.0 (the user runs `release.cmd`).
