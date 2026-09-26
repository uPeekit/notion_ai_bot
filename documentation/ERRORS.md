# Errors and Recovery

Principle: clarification is not an error. Errors below are things the pipeline cannot resolve with the user.

## Classes

| Code | Where | Cause | User message (ru) | Recovery |
|---|---|---|---|---|
| `AUTH_DENIED` | telegram/auth | user id not allowlisted | none | log WARNING, ignore |
| `STT_EMPTY` | speech | no speech detected | Не разобрал голосовое сообщение. Повторите или напишите текстом. | none |
| `STT_FAILED` | speech | model load / decode exception | Ошибка распознавания речи. | audit error; if CUDA error → auto-switch to CPU for process lifetime |
| `DISCOVERY_FAILED` | notion/discovery | HTTP/network error on any step | Notion недоступен. | use stale snapshot if < 1 h; else reply and stop |
| `LLM_UNAVAILABLE` | llm/ollama | connection refused / timeout | Локальная модель недоступна. Попробуйте позже. | none; startup check warns if model missing (`ollama pull` hint in log) |
| `LLM_INVALID_OUTPUT` | llm/ollama → interpretation | JSON invalid or Pydantic fails | Не удалось разобрать запрос. | 1 retry with error text appended; both responses audited |
| `INTENT_UNKNOWN` | validation/semantic | LLM `intent.value == "unknown"` | Не понял, что нужно сделать в Notion. | REJECT immediately, no candidates built |
| `SEM_UNKNOWN_KEY` | validation/semantic | target/field/item/option key not in snapshot | Не удалось сопоставить запрос с Notion. | REJECT; indicates schema/enum fallback issue → log ERROR |
| `SEM_TYPE` | validation/semantic | value type mismatch (e.g. date not ISO) | Не удалось разобрать значение поля «…». | REJECT; «…» is the field name, carried to the reply on `Issue.detail` |
| `SEM_STATUS_CLEAR` | validation/semantic | `explicit_null` on a `status` field (Notion API cannot clear a status) | — (not user-visible) | field dropped (left unwritten), warning logged |
| `SEM_UNSUPPORTED_OP` | validation/semantic | operation not in target.operations | Эта операция недоступна для «…». | REJECT; «…» is the target name, carried to the reply on `Issue.detail` |
| `SEM_READONLY_FIELD` | reserved, unreachable | readonly fields never get a context key (`ContextBuilder._fields` skips `not f.writable`), so the LLM can never name one | — | none |
| `nothing_to_write` | validation/policy | `update` with a resolved item but no field `value`/`explicit_null` | Не понял, что именно изменить. | REJECT, carries the candidate + a `Question("nothing_to_write", ...)` |
| `NOTION_4XX` | notion/direct | validation error from Notion | Notion отклонил операцию: <message>. | REJECT after execute attempt; audit |
| `NOTION_429` | notion/direct | rate limited | none until exhausted | retry ×3 with Retry-After |
| `NOTION_5XX` | notion/direct | server error | Notion временно недоступен. | retry ×2 with backoff |
| `UNDO_EXPIRED` | commands/executor | undo window passed | Отменить уже нельзя (прошло больше N минут). | none |
| `UNDO_FAILED` | commands/executor | Notion refused undo | Не удалось отменить: <message>. | audit |
| `SESSION_EXPIRED` | conversation/session | button pressed after TTL, or a stale/mismatched callback token, or an unknown callback prefix | Вопрос устарел. Повторите запрос. | drop session |
| `REWRITE_EMPTY` | commands/executor | the page or note holds no text, only pictures / files / sub-pages | На «…» нет текста, который можно переписать | nothing written, nothing archived |
| `REWRITE_NOTHING` | llm/edits | the model read the page and found nothing the instruction applies to | Не нашёл на «…», что именно поправить | nothing written |
| `REWRITE_UNAVAILABLE` | commands/executor | no rewriter (no Anthropic key, or cloud off) | Переписать текст сейчас не могу | none |
| `REWRITE_FAILED` | llm/rewrite | Claude refused, answered nothing, or was cut off | Не удалось переписать текст (…) | the page is untouched; the credit/key warning is appended once an hour |
| `INTERNAL` | conversation/orchestrator | unexpected exception on any path (the orchestrator never raises to the transport) | Не удалось обработать сообщение. | audit error, log exception |
| `CONFIG_INVALID` | config | missing env / bad value | process exits with message | fix `.env` |
| `INBOX_SAVED` | conversation/orchestrator (`_to_inbox`) | the inbox fallback wrote successfully | Сохранил в «{target}»: {url} | informational only — not an `events.error` value; the reply carries an Undo button like any other write |
| `INBOX_FAILED` | conversation/orchestrator — audited as `events.error` from `_sweep`, from `_callback`'s `inbox` verb (the `[В разное]` button), and as a *fallback* from `_expired_prefix` (F18's next-message rescue) | an inbox target is flagged but the fallback write itself failed (Notion error, or a database inbox with no title property) | Не удалось сохранить в «{target}». | the first two set `events.error` outright. `_expired_prefix` sets it only if the turn records no error of its own: that row belongs to the *new* message, which may well succeed, so without it a lost rescued message would leave no trace in the audit log at all — the one blind spot the inbox exists to prevent. `_to_inbox` inside `_inbox_or_error` (F17's failed-save case) still audits nothing of its own, deliberately: the row keeps the original failure code, which says more about what went wrong |
| `INBOX_NOT_CONFIGURED` | conversation/orchestrator (`_inbox_target`) | `INBOX_MODE=off`, or mode `button` outside a button press, or no target flagged and no `INBOX_TARGET_ID` override | — (no reply of its own; the original error/REJECT message stands alone, with no `[В разное]` offer) | not an audited code — documents why the inbox never engages; flag a target in `targets.yaml` or set `INBOX_TARGET_ID` |

Both of these REJECTs are raised by the validator, so the `Decision` reaching the orchestrator carries
no candidate to name the field or target from. `Issue.detail` is that one user-facing noun, kept apart
from `Issue.message` (English, for the audit log); `orchestrator.REJECT_DETAIL` maps each code to the
placeholder it fills. A message whose placeholder cannot be filled degrades to `INTENT_UNKNOWN`, so
adding a placeholder to a template without a source for it silently hides the message.

## Inbox fallback

Rather than dropping a message the pipeline could not resolve, the orchestrator appends it to the
one Notion target the user flagged as the inbox (`targets.yaml` `inbox: true`, or the
`INBOX_TARGET_ID` override). Codes that fall back: `LLM_UNAVAILABLE`, `LLM_INVALID_OUTPUT`, every
REJECT (`INTENT_UNKNOWN`, `SEM_UNKNOWN_KEY`, `SEM_TYPE`, `SEM_UNSUPPORTED_OP`, `nothing_to_write`,
`item_not_found` once its own question goes unanswered), `NOTION_4XX`/`NOTION_5XX` on execute, an
expired unanswered session, the `[В разное]` button, and a clarification budget (`MAX_QUESTIONS`)
that ran out without a resolution. Explicitly not: `DISCOVERY_FAILED` (there is nothing to write
to), and never after a successful write. See ARCHITECTURE.md §4/§7.

## Startup checks (`main.py`)

Steps 1-2 are synchronous and run in `main()` before any event loop exists. Steps 3-5 and 7 are
async and run from `Application.post_init` — i.e. inside python-telegram-bot's own event loop,
the same one that later polls — never from a throwaway `asyncio.run(...)` of their own: a second,
separate loop would leave the same httpx-backed Notion/Ollama clients holding idle keep-alive
connections bound to it, and the first real call on them once polling starts would raise
`RuntimeError: Event loop is closed`.

0. `logging_setup.configure(LOG_LEVEL, redact=(telegram token, notion token), log_file=LOG_FILE)`;
   an unknown `LOG_LEVEL` → exit 2 naming the valid levels (the name is normalised to upper case
   first, so `LOG_LEVEL=info` is accepted rather than crashing on `Logger.setLevel`).
0a. `InstanceLock(<db dir>/bot.pid).acquire()`; another process already holding it → exit 5. One
   bot per database: two pollers on one token get Telegram `409 Conflict`s and race each other's
   sessions. The lock is an OS byte-range lock (released by the OS when the process dies, crash
   included), not the file's existence — a `bot.pid` left behind never blocks a restart.
1. Settings load (`Settings.require_telegram()`); missing required → exit 2.
2. `AuditStore.assert_schema_current()`; a pending migration → exit 4 with the hint to run the
   updater. Migrations are never applied here — never `AuditStore.migrate()` — only by the
   installer/updater or `tools/migrate.py` (ARCHITECTURE.md §15).
3. Ollama `GET /api/tags`; warn if `LLM_MODEL` missing or the call itself fails — not fatal, the
   user may start Ollama or pull the model later.
4. Notion `GET /v1/users/me`; exit 3 on 401/403 (auth failure). Any other failure (a 5xx, a
   timeout) only warns and lets startup continue, the same as the Ollama check above — it is not
   proof the token is wrong, and discovery below will surface it again if it persists.
5. Initial discovery; write `targets.yaml`; log the target count and which target is flagged as
   the inbox, or warn that none is (the fallback is then inert) — not fatal on its own failure.
6. Whisper not loaded at startup (lazy).
7. `set_my_commands` publishes the "/" command menu; a failure only warns (and the warning names
   the exception class only — an `InvalidToken` raised here carries the bot token in its text).
8. Admin server start (if `ADMIN_UI_PORT > 0`); an `OSError` binding the port (already in use, or
   a Windows excluded port range — `WinError 10013` on 8787 is common) only warns and startup
   continues. The page is optional by design; losing it must not lose the bot.
9. Telegram polling start. If Telegram rejects the bot token, python-telegram-bot raises
   `InvalidToken` out of `run_polling()` → exit 2 naming `TELEGRAM_BOT_TOKEN`. Neither that
   exception's message nor its traceback is ever printed or logged: PTB builds it as
   ``InvalidToken(f"The token `{token}` was rejected by the server.")``. PTB's own ERROR line
   ("Invalid token. Aborting retry loop.") is kept, but the formatter drops its ~40-line
   traceback — a rejected token is a config mistake, not a crash.

Exit codes: `0` stopped normally, `2` configuration (`.env`, or a token Telegram rejected), `3`
Notion auth, `4` pending migration, `5` already running. `deploy/start.ps1` (behind `start.cmd`)
turns each into one plain-English line and, for `2`/`3`, offers to open `.env`; for `4` it offers
to run `tools.migrate --apply` on an explicit yes — still never implicitly.
`tests/test_deploy_scripts.py` fails if `main.py` gains an `EXIT_*` the launcher doesn't explain.

## Logging

Structured `logging` with `event_id` in every record after an event is created. Never log tokens,
never log message text above DEBUG (`tests/test_security.py` enforces exactly that: over a full
text → execute → undo flow, no line above `DEBUG` may contain the message text). DEBUG may log
LLM context and response.

`app.logging_setup.configure()` also floors two third-party loggers to WARNING no matter what
`LOG_LEVEL` is set to, because each would otherwise print the Telegram bot token to stderr on its
own, with no app code calling `log.*` on it: `httpx`/`httpcore` (python-telegram-bot's own HTTP
client logs every request's full URL, token and all, at INFO) and `telegram.ext.ExtBot` (logs the
same token-bearing URL once at DEBUG, from its own constructor — not an HTTP request, so the
httpx floor does nothing for it).

Records also go to `LOG_FILE` (default `logs/bot.log`, rotated at 5 MB × 5; empty disables it)
through the same filter and the same redacting formatter as stderr — a file is where a leaked
token would outlive the console window.

Neither floor can help with the third leak, so `configure()` also installs a redacting formatter
over the stderr handler (and the file handler): every occurrence of a secret *value* it was explicitly given — `main()`
passes the Telegram and Notion token values, and nothing else; `configure` never sees a `Settings`
— becomes `***` in the formatted record, message, interpolated args and traceback alike. The leak
it exists for is `telegram.ext`'s polling retry loop
(`telegram/ext/_utils/networkloop.py`), which logs the token-bearing `InvalidToken` at ERROR
*with* `exc_info` when Telegram rejects the token — an ERROR on a logger the app genuinely wants
to hear from, so no level floor could have stopped it. See `app/logging_setup.py`'s module
docstring.
