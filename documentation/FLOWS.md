# Flows

All bot replies in Russian. Buttons in `[brackets]`.

## F1. Create item, unambiguous

1. User (voice): «купи молоко в Рими»
2. Transcript stored in audit (not shown).
3. Snapshot fetched (or cached). LLM: intent create 0.97; candidates: Покупки 0.95 {Название: Молоко, Магазин: Rimi}, Задачи 0.60.
4. Policy: margin 0.35 ≥ 0.10, fields valid → EXECUTE.
5. Notion `POST /v1/pages`.
6. Bot: `✅ Покупки: Молоко · Магазин: Rimi` + `[Открыть]` (Notion URL) `[Отменить]`. Reply text reflects the properties actually written, read back from the Notion response.
7. `[Отменить]` within 5 min → page archived → `↩️ Отменено`.

## F2. Create, target ambiguous

1. «добавь хлеб»
2. LLM: Покупки 0.88, Задачи 0.82. Margin 0.06 < 0.10 → CLARIFY(target).
3. Bot: `Куда добавить «хлеб»?` `[Покупки] [Задачи] [Отмена]`
4. `[Покупки]` → Resolver sets target=t1, uses that candidate's fields → validate → EXECUTE → F1 step 6.

## F3. Create, required field missing

1. «добавь задачу подготовить документы» — Задачи has required Приоритет (marked in targets.yaml).
2. Policy: required field not_mentioned → CLARIFY(field_required).
3. Bot: `Задачи: «Подготовить документы». Какой приоритет?` `[A] [B] [C] [Пропустить] [Отмена]`
   `[Пропустить]` is shown only when the field is not the title; it stores explicit_null and the policy still rejects if the field is required. For required fields the button is hidden.
4. `[A]` → validate → EXECUTE.

## F4. Free-text answer to a button question

1. Bot asked F3 step 3.
2. User (voice): «высокий, и срок пятница»
3. A live session for this chat makes the message a free-text answer. The orchestrator adds a
   `pending` block to the LLM context — `{"вопрос": "<the question text as shown>", "цель":
   "<target name or empty>", "исходный_текст": "<the original request>"}`, names only, never a
   Notion id or context key — and prompts with `original_text + "\n" + new_text`. `MAX_QUESTIONS`
   does not apply here: a free-text answer is indistinguishable from a fresh request and is never
   refused for it. LLM returns Приоритет=A (0.9), Срок=2026-09-11 (0.9).
4. Validate → EXECUTE. Session cleared.

## F5. Unrelated message while a question is pending

1. Bot asked F2 step 3.
2. User: «запиши идею: попробовать новый маршрут».
3. LLM sees pending question; returns candidate for Идеи (page) intent append, and a flag that the pending question is unanswered (candidate target ≠ pending targets). Orchestrator drops the old session, replies `Предыдущий вопрос отменён.` then handles the new interpretation normally.

## F6. Update by reference

1. «отметь молоко купленным»
2. Context includes items t1.i7 «Молоко». LLM: intent update 0.95; candidate t1, item t1.i7, fields {Куплено: true}.
3. Policy → EXECUTE. `PATCH /v1/pages/{id}`; previous value recorded for undo.
4. Bot: `✅ Покупки: Молоко · Куплено: да` `[Открыть] [Отменить]`.

## F7. Update, item ambiguous

1. «отметь молоко купленным», items contain «Молоко 2 л» and «Молоко овсяное».
2. LLM: item null, item_candidates [t1.i7, t1.i9] → CLARIFY(item).
3. Bot: `Какой элемент?` `[Молоко 2 л] [Молоко овсяное] [Отмена]`.
4. Pick → EXECUTE.

## F8. Update, item not found

1. «отметь кефир купленным», no such item in context.
2. LLM: item null, item_candidates []. Policy → REJECT, but carries the candidate and one
   `Question("item_not_found", proposed="кефир")` so the conversation layer can answer it without
   re-running the LLM.
3. Bot: `Не нашёл «кефир» в списке.` `[Добавить как новое] [Отмена]` (plus `[В разное]` when an
   inbox target is available).
4. `[Добавить как новое]` → `apply_answer` flips the intent to `create`, sets the title field to
   the unmatched text («кефир»), drops the stale item id → re-validate → EXECUTE, same reply shape
   as F1 (`✅ Добавлено: Покупки — Кефир` + `[Открыть] [Отменить]`) — no second LLM call.

## F9. Append to page

1. «в идеи: попробовать сыр с плесенью»
2. LLM: intent append; target t2 (page Идеи), item null (append to page itself), content «Попробовать сыр с плесенью».
3. EXECUTE `PATCH /v1/blocks/{page_id}/children` paragraph. Undo = delete created block ids.
4. Bot: `✅ Идеи: добавлен абзац` `[Открыть] [Отменить]`.

## F10. Create sub-page

1. «создай страницу отпуск 2027 в идеях, там будет план поездки»
2. LLM: intent create; target t2 kind page; fields {title: «Отпуск 2027»}; content «План поездки».
3. EXECUTE `POST /v1/pages` parent page_id + paragraph. Undo = archive.

## F11. Search

1. «что у меня в покупках на Rimi?»
2. LLM: intent search; target t1; search_query «Rimi» (or field filter Магазин=Rimi when expressible).
3. App queries data source (title contains, or select equals when the LLM set a field value). Reply: numbered titles with links, max 20.

## F12. Date low confidence

1. «сделать отчёт к понедельнику» on a Saturday.
2. LLM: Срок 2026-09-14 confidence 0.7 < 0.80 → CLARIFY(date).
3. Bot: `Срок — понедельник, 14 сентября?` `[Да] [Другая дата] [Без срока] [Отмена]`.
4. `[Другая дата]` → bot asks for free text; next message handled as F4.

## F13. Voice transcription failure

1. Empty or unintelligible audio → transcript empty.
2. Bot: `Не разобрал голосовое сообщение. Повторите или напишите текстом.` No LLM call, event audited.

## F14. Infrastructure errors

| Failure | Bot reply | Audit |
|---|---|---|
| Ollama unreachable / timeout | `Локальная модель недоступна. Попробуйте позже.` | error |
| LLM output fails Pydantic | one automatic retry with the validation error appended to the prompt; then `Не удалось разобрать запрос.` | both raw responses |
| Notion 4xx on execute | `Notion отклонил операцию: <short reason>` | error |
| Notion 429 | retry with `Retry-After` up to 3 times, then as 4xx | error |
| Discovery fails | use last snapshot if < 1 h old with warning line; else `Notion недоступен.` | error |

## F15. Admin flow

1. Start bot → startup discovery → `data/targets.yaml` populated with all visible targets.
2. Open `http://127.0.0.1:8787` → tree, edit descriptions and `required` flags → Save.
3. Save writes YAML and invalidates cache; next message uses new descriptions.
4. Telegram `/refresh` → forced discovery, replies count of targets. `/targets` → text tree. `/undo` → undo last execution if within window. `/cancel` → drop pending session. `/start`, `/help` → usage text.

## F16. Unauthorized user

Any update from a user id outside `TELEGRAM_ALLOWED_USER_IDS` is ignored (no reply) and logged at WARNING without message content.

## F17. Не понял → в разное

1. «расскажи анекдот» — nothing here maps to an intent.
2. LLM: `intent.value == "unknown"` → `SemanticValidator` issue `INTENT_UNKNOWN` → Policy → REJECT with no candidate. `_dispatch` has nothing to ask about, so it falls straight to the inbox fallback — the same path a REJECT with no candidate, an invalid/unavailable LLM response, a Notion error during execute, or an exhausted clarification budget (`MAX_QUESTIONS`) all take. Never a bare discovery failure (nothing to write to), never after a successful write.
3. Mode `auto`, inbox flagged on «Разное»: bot replies `Не понял, что нужно сделать в Notion. Сохранил в «Разное»: <url>` `[Отменить]` (one message — the error and the save share one line, joined by a space).
4. Mode `button` (or the save itself failed): bot replies the plain error alone, `Не понял, что нужно сделать в Notion.`, with `[В разное]` attached; pressing it replays this event's own text (`i:<event_id>`) into the same save.

## F18. Вопрос устарел → в разное

1. Bot asked a CLARIFY question (e.g. F3 step 3) and the user does not answer within `SESSION_TTL_S` (900 s default).
2. The user later sends an unrelated message, e.g. «купи молоко». Before touching the (now stale) session, the orchestrator checks whether it already expired, rescues its `original_text` to the inbox target, then handles the new message normally.
3. Mode `auto`, inbox flagged on «Разное»: bot replies with the rescue line on its own line, followed by the reply to the new message:
   ```
   Вопрос устарел — сохранил сообщение в «Разное».
   ✅ Добавлено: Покупки — Молоко
   Открыть: https://notion.so/…
   ```
4. If nobody sends a follow-up message at all, the sweeper (`Orchestrator.flush_expired_sessions`, scheduled by Plan 3b) rescues the same text on its own; there is no reply to show since there is no chat turn to attach it to.
5. Mode `button`: the expired session's text is dropped silently — an accepted limitation of that mode, since there is no button left to press. Mode `off`: same, always. Only `auto` (the default) rescues it without the user asking.
