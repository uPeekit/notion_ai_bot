# Flows

All bot replies in Russian. Buttons in `[brackets]`.

## F1. Create item, unambiguous

1. User (voice): «купи молоко в Рими»
2. Transcript stored in audit (not shown).
3. Snapshot fetched (or cached). LLM: intent create 0.97; candidates: Покупки 0.95 {Название: Молоко, Магазин: Rimi}, Задачи 0.60.
4. Policy: margin 0.35 ≥ 0.10, fields valid → EXECUTE.
5. Notion `POST /v1/pages`.
6. Bot (`format_execution` on a `CreateItem` result — `DONE_CREATE_ITEM` header naming the target and the title value, one `• name: value` bullet per other written property, then `DONE_LINK` as a plain text line, not a button):
   ```
   ✅ Добавлено: Покупки — Молоко
   • Магазин: Rimi
   Открыть: <url>
   ```
   `[Отменить]` (the only button — "Открыть" is a text line the client may auto-link, never a `Button`). The bullet/header values are the properties the app itself sent (`cmd.properties`), not a re-read of Notion's response; only `page_id`/`url` come back from Notion.
7. `[Отменить]` within 5 min → page archived → `↩️ Отменено.`

## F2. Create, target ambiguous

1. «добавь хлеб»
2. LLM: Покупки 0.88, Задачи 0.82. Margin 0.06 < 0.10 → CLARIFY(target).
3. Bot: `Уточните, к какой записи это относится.` `[Покупки] [Задачи] [Отмена]` (+ `[В разное]` when an inbox target is available). The `target` question has no target-name variant — it's the one asking which target — so this text never changes with the candidate.
4. `[Покупки]` → `apply_answer` narrows the session to the chosen candidate (confidence forced to 1.0) → `result_from_session` re-validates → EXECUTE → F1 step 6.

## F3. Create, required field missing

1. «добавь задачу подготовить документы» — Задачи has required Приоритет (marked in targets.yaml).
2. Policy: required field not_mentioned → CLARIFY(field_required).
3. Bot: `Задачи: какое значение указать для поля «Приоритет»?` `[A] [B] [C] [Отмена]` (+ `[В разное]`) — `field_required` is one of the four question types with a target-naming variant, so the target is always named when it's known. `[A]`/`[B]`/`[C]` stand in for the field's own Notion select/status option names (whatever the user actually named them), not fixed text. There is no "skip" button: `field_required` only fires for a `create`-intent required field, and the app has no button that stores `explicit_null` for it — a required field can only be answered, by button (when it has options) or by free text otherwise.
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
2. User: «запиши идею: попробовать новый маршрут» — unrelated to the pending target question.
3. The orchestrator does not distinguish "an answer" from "an unrelated message": both are handled exactly like F4 — the same `pending` block is added to the context and the LLM is prompted with `session.original_text + "\n" + new_text` in one call. There is no separate branch that detects the message is unrelated and no meta-reply telling the user so (no such string exists in `texts.py`); the fresh interpretation simply replaces the session outright and whatever `Policy` decides for it (EXECUTE/CLARIFY/REJECT) becomes the only reply.
4. Here the model reads the new sentence as a complete request on its own — intent `append`, target Идеи — and Policy → EXECUTE. Bot replies as in F9 (`✅ Дописано: Идеи — Идеи` + `Открыть: <url>` + `[Отменить]`); the old target question and its session are simply gone, dropped as a side effect of the EXECUTE branch.

## F6. Update by reference

1. «отметь молоко купленным»
2. Context includes items t1.i7 «Молоко». LLM: intent update 0.95; candidate t1, item t1.i7, fields {Куплено: true}.
3. Policy → EXECUTE. `PATCH /v1/pages/{id}`; previous value recorded for undo.
4. Bot (`DONE_UPDATE` header naming the target and the item's own title; unlike a create, every written property becomes a bullet, the title included):
   ```
   ✅ Обновлено: Покупки — Молоко
   • Куплено: Да
   Открыть: <url>
   ```
   `[Отменить]`.

## F7. Update, item ambiguous

1. «отметь молоко купленным», items contain «Молоко 2 л» and «Молоко овсяное».
2. LLM: item null, item_candidates [t1.i7, t1.i9] → CLARIFY(item).
3. Bot: `Какой элемент в «Покупки»?` `[Молоко 2 л] [Молоко овсяное] [Отмена]` (+ `[В разное]`) — `item` is one of the four question types with a target-naming variant, and the target is always known by this point, so the plain `Какой элемент?` wording never actually appears.
4. Pick → EXECUTE (F6-style reply).

## F8. Update, item not found

1. «отметь кефир купленным», no such item in context.
2. LLM: item null, item_candidates []. Policy → REJECT, but carries the candidate and one
   `Question("item_not_found", proposed="кефир")` so the conversation layer can answer it without
   re-running the LLM.
3. Bot: `Не нашёл «кефир» в списке.` `[Добавить как новое] [Отмена]` (plus `[В разное]` when an
   inbox target is available).
4. `[Добавить как новое]` → `apply_answer` flips the intent to `create`, sets the title field to
   the unmatched text («кефир»), drops the stale item id → re-validate → EXECUTE, same reply shape
   as F1:
   ```
   ✅ Добавлено: Покупки — Кефир
   Открыть: <url>
   ```
   `[Отменить]` — no second LLM call.

## F9. Append to page

1. «в идеи: попробовать сыр с плесенью»
2. LLM: intent append; target t2 (page Идеи), item null (append to page itself), content «Попробовать сыр с плесенью».
3. EXECUTE `PATCH /v1/blocks/{page_id}/children` paragraph. Undo = delete created block ids.
4. Bot (`DONE_APPEND`; `AppendBlocks` never produces bullets — the paragraph text itself isn't echoed back, only the fact that something was written):
   ```
   ✅ Дописано: Идеи — Идеи
   Открыть: <url>
   ```
   `[Отменить]`. The target name and the "item" title are both the page's own name when appending to the page itself (`AppendBlocks.target_name`/`page_title` are the same `t.name`) — the repetition is what the code actually produces, not a typo.

## F10. Create sub-page

1. «создай страницу отпуск 2027 в идеях, там будет план поездки»
2. LLM: intent create; target t2 kind page; fields {title: «Отпуск 2027»}; content «План поездки».
3. EXECUTE `POST /v1/pages` parent page_id + paragraph. Undo = archive.
4. Bot (`DONE_CREATE_PAGE`; `CreatePage` never produces bullets either):
   ```
   ✅ Создано: Идеи — Отпуск 2027
   Открыть: <url>
   ```
   `[Отменить]`.

## F11. Search

1. «что у меня в покупках на Rimi?»
2. LLM: intent search; target t1; search_query «Rimi» (or field filter Магазин=Rimi when expressible).
3. App queries data source (title contains, or select equals when the LLM set a field value). `format_search` renders at most 20 hits, one per line, no buttons at all (a `Search` never produces an `UndoRecord`, so there is nothing to undo):
   ```
   Нашёл:
   1. <title> — <url>
   2. <title> — <url>
   ```
   Zero hits: `Ничего не нашёл.` instead.

## F12. Date low confidence

1. «сделать отчёт к понедельнику» on a Saturday.
2. LLM: Срок 2026-09-14 confidence 0.7 < 0.80 → CLARIFY(date).
3. Bot: `Дата «Срок»: 14.09.2026. Верно?` `[Да] [Другое] [Отмена]` (+ `[В разное]`) — these are the
   real `date`-question extras (`reply._EXTRAS["date"]`: confirm/other) plus cancel/inbox; there is
   no third "skip the date" button.
4. `[Другое]` → `apply_answer` returns the `"free_text"` verb, bot replies `Введите значение.`
   (`ENTER_VALUE`); the next message is handled as F4.

## F13. Voice transcription failure

1. Empty or unintelligible audio → transcript empty.
2. Bot: `Не разобрал голосовое сообщение. Повторите или напишите текстом.` No LLM call, event audited.

## F14. Infrastructure errors

| Failure | Bot reply | Audit |
|---|---|---|
| Ollama unreachable / timeout | `Локальная модель недоступна. Попробуйте позже.` (+ inbox fallback, see F17) | error |
| LLM output fails Pydantic | one automatic retry with the validation error appended to the prompt; then `Не удалось разобрать запрос.` (+ inbox fallback, see F17) | both raw responses |
| Notion 4xx on execute | `Notion отклонил операцию: <short reason>` (+ inbox fallback — the write did not happen, so the text isn't lost) | error |
| Notion 429 | retry with `Retry-After` up to 3 times, then as 4xx | error |
| Discovery fails | use last snapshot if < 1 h old with warning line; else `Notion недоступен.` (no inbox fallback here — nothing to write to) | error |

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
3. Mode `auto`, inbox flagged on «Разное», save succeeds: bot replies `Не понял, что нужно сделать в Notion. Сохранил в «Разное»: <url>` `[Отменить]` (one message — the error and the save share one line, joined by a space).
4. Mode `auto`, but the save itself fails (Notion error, or the inbox has no title property): the same one-line join, with the failure text instead — `Не понял, что нужно сделать в Notion. Не удалось сохранить в «Разное».` — with `[В разное]` attached as a retry offer (pressing it replays this event's own text, `i:<event_id>`, into another save attempt; no `[Отменить]`, since nothing was written).
5. Mode `button` (the save is never attempted at all): bot replies the plain error alone, `Не понял, что нужно сделать в Notion.`, with `[В разное]` attached; pressing it replays this event's own text (`i:<event_id>`) into the first save attempt.

6. Whichever path wrote it, the inbox page also gets a short note saying *why* the message is
   there, so it can be triaged later: `Причина: <the error text the user was shown>`, or
   `Остался без ответа вопрос: <the question, worded exactly as it was asked>` for a rescued
   session. A page inbox keeps the note as a second paragraph under the text (so an Undo removes
   both blocks); a database inbox stores the text as the row title only, and the note is dropped
   — there is no property to put it in.
7. Pressing `[В разное]` on a pending question and having the save fail does not destroy the
   question: the reply is the failure line with the question repeated under it, carrying the same
   keyboard and the same token, so `[В разное]` is its own retry (`ERRORS.md`: `INBOX_FAILED`).

## F18. Вопрос устарел → в разное

1. Bot asked a CLARIFY question (e.g. F3 step 3) and the user does not answer within `SESSION_TTL_S` (900 s default).
2. The user later sends an unrelated message, e.g. «купи молоко». Before touching the (now stale) session, the orchestrator checks whether it already expired, rescues its `original_text` to the inbox target, then handles the new message normally.
3. Mode `auto`, inbox flagged on «Разное»: bot replies with the rescue line on its own line, followed by the reply to the new message:
   ```
   Вопрос устарел — сохранил сообщение в «Разное».
   ✅ Добавлено: Покупки — Молоко
   Открыть: https://notion.so/…
   ```
4. If nobody sends a follow-up message at all, the sweeper (`Orchestrator.flush_expired_sessions`, scheduled by Plan 3b) rescues the same text on its own; there is no reply to show since there is no chat turn to attach it to. It takes each chat's own lock around that chat's pop-and-rescue, so a turn already in flight for that chat finishes first and the message cannot be handled and rescued at the same time.
5. Mode `button`: the expired session's text is dropped silently — an accepted limitation of that mode, since there is no button left to press. Mode `off`: same, always. Only `auto` (the default) rescues it without the user asking.
