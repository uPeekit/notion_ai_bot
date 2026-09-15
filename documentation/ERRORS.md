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
| `INTERNAL` | conversation/orchestrator | unexpected exception on any path (the orchestrator never raises to the transport) | Не удалось обработать сообщение. | audit error, log exception |
| `CONFIG_INVALID` | config | missing env / bad value | process exits with message | fix `.env` |
| `INBOX_SAVED` | conversation/orchestrator (`_to_inbox`) | the inbox fallback wrote successfully | Сохранил в «{target}»: {url} | informational only — not an `events.error` value; the reply carries an Undo button like any other write |
| `INBOX_FAILED` | conversation/orchestrator — audited as `events.error` only from `_sweep` and `_callback`'s `inbox` verb (the `[В разное]` button) | an inbox target is flagged but the fallback write itself failed (Notion error, or a database inbox with no title property) | Не удалось сохранить в «{target}». | the two sites above set `events.error`; every other path that shows this text — `_to_inbox` inside `_inbox_or_error` (e.g. F17's failed-save case), and `_expired_prefix` (F18's next-message rescue) — renders it to the user without auditing it itself, leaving `events.error` holding whatever code the original failure used (or unset) |
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

1. Settings load; missing required → exit 2.
2. SQLite migrate.
3. Ollama `GET /api/tags`; warn if `LLM_MODEL` missing.
4. Notion `GET /v1/users/me`; exit 3 on 401.
5. Initial discovery; write `targets.yaml`; log target count.
6. Whisper not loaded at startup (lazy).
7. Admin server start (if port > 0).
8. Telegram polling start.

## Logging

Structured `logging` with `event_id` in every record after an event is created. Never log tokens, never log message text above INFO. DEBUG may log LLM context and response.
