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
| `SEM_TYPE` | validation/semantic | value type mismatch (e.g. date not ISO) | Не удалось разобрать значение «…». | REJECT |
| `SEM_STATUS_CLEAR` | validation/semantic | `explicit_null` on a `status` field (Notion API cannot clear a status) | — (not user-visible) | field dropped (left unwritten), warning logged |
| `SEM_UNSUPPORTED_OP` | validation/semantic | operation not in target.operations | Эта операция недоступна для «…». | REJECT |
| `SEM_READONLY_FIELD` | reserved, unreachable | readonly fields never get a context key (`ContextBuilder._fields` skips `not f.writable`), so the LLM can never name one | — | none |
| `nothing_to_write` | validation/policy | `update` with a resolved item but no field `value`/`explicit_null` | Не понял, что именно изменить. | REJECT, carries the candidate + a `Question("nothing_to_write", ...)` |
| `NOTION_4XX` | notion/direct | validation error from Notion | Notion отклонил операцию: <message>. | REJECT after execute attempt; audit |
| `NOTION_429` | notion/direct | rate limited | none until exhausted | retry ×3 with Retry-After |
| `NOTION_5XX` | notion/direct | server error | Notion временно недоступен. | retry ×2 with backoff |
| `UNDO_EXPIRED` | commands/executor | undo window passed | Отменить уже нельзя (прошло больше N минут). | none |
| `UNDO_FAILED` | commands/executor | Notion refused undo | Не удалось отменить: <message>. | audit |
| `SESSION_EXPIRED` | conversation/session | button pressed after TTL | Вопрос устарел. Повторите запрос. | drop session |
| `CONFIG_INVALID` | config | missing env / bad value | process exits with message | fix `.env` |

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
