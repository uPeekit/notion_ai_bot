"""Every Russian user-facing string the bot can send, in one place. The only other files
allowed to hold Cyrillic literals are app/llm/prompts.py (the LLM system prompt) and
app/llm/context.py (weekday names) — those speak Russian to the local model, not to the user.

This module is a leaf: it imports only app.validation.policy, for the QType type used to
annotate QUESTION. app.conversation.reply.py and app.notion.props.py both import from here."""

from __future__ import annotations

from app.validation.policy import QType

# ---- buttons ----------------------------------------------------------------------------------

BTN_CANCEL = "Отмена"
BTN_INBOX = "В разное"
BTN_UNDO = "Отменить"
BTN_CONFIRM = "Да"
BTN_OTHER = "Другое"
BTN_ADD_NEW = "Добавить как новое"

# ---- values -------------------------------------------------------------------------------

BOOL_YES = "Да"
BOOL_NO = "Нет"
FIELD_CLEARED = "очищено"  # a Written.value of None: the field was explicitly cleared (e.g.
                            # "убери магазин у молока"), not missing/undefined
UNTITLED = "(без названия)"  # title fallback for a page with no title text; shared with
                              # app.notion.props.page_title

# ---- clarification questions, keyed by Question.type (QType) --------------------------------

QUESTION: dict[QType, str] = {
    "target": "Уточните, к какой записи это относится.",
    "intent_confirm": "Похоже, вы хотите {intent}. Верно?",
    "item": "Какой элемент?",
    "item_not_found": "Не нашёл «{item_text}» в списке.",
    "field_required": "Какое значение указать для поля «{field_name}»?",
    "field_ambiguous": "Уточните значение поля «{field_name}»:",
    "date": "Дата «{field_name}»: {value}. Верно?",
    "field_confirm": "«{field_name}»: {value}. Верно?",
    "content_required": "Что написать?",
    "nothing_to_write": "Не понял, что именно изменить.",
}

# Target-naming variants of four of the templates above, for when the caller knows the target's
# name (FLOWS.md's wording names it, e.g. "...в «Покупки»"). A separate template — rather than
# an optional segment inside QUESTION[q.type] — keeps the target-less default free of dangling
# quotes or empty «» when no name is available.
QUESTION_WITH_TARGET: dict[QType, str] = {
    "item": "Какой элемент в «{target_name}»?",
    "field_required": "{target_name}: какое значение указать для поля «{field_name}»?",
    "content_required": "Что написать в «{target_name}»?",
    "nothing_to_write": "Не понял, что именно изменить в «{target_name}».",
}

# Question.proposed for intent_confirm carries the raw intent value ("create"/"update"/
# "append"/"search"); this maps it to a Russian verb phrase for the question text.
INTENT_LABELS: dict[str, str] = {
    "create": "добавить запись",
    "update": "изменить запись",
    "append": "дописать текст",
    "search": "найти",
}

# ---- execution results ------------------------------------------------------------------------

DONE_CREATE_ITEM = "✅ Добавлено: {target_name} — {item_title}"
DONE_UPDATE = "✅ Обновлено: {target_name} — {item_title}"
DONE_CREATE_PAGE = "✅ Создано: {target_name} — {item_title}"
DONE_APPEND = "✅ Дописано: {target_name} — {item_title}"
DONE_LINK = "Открыть: {url}"

SEARCH_HEADER = "Нашёл:"
SEARCH_EMPTY = "Ничего не нашёл."

# ---- standalone replies -----------------------------------------------------------------------

# The inbox target is whatever the user flagged in targets.yaml, so all three of these name it
# at send time rather than baking one name into the sentence.
INBOX_SAVED = "Сохранил в «{target_name}»: {url}"
INBOX_SAVED_EXPIRED = "Вопрос устарел — сохранил сообщение в «{target_name}»."
INBOX_FAILED = "Не удалось сохранить в «{target_name}»."
CANCELLED = "Отменено."
UNDONE = "↩️ Отменено."
ENTER_VALUE = "Введите значение."  # answer to [Другое]: the next message is free text (F12→F4)

# ---- errors (documentation/ERRORS.md), keyed by error code ------------------------------------

ERRORS: dict[str, str] = {
    "STT_EMPTY": "Не разобрал голосовое сообщение. Повторите или напишите текстом.",
    "STT_FAILED": "Ошибка распознавания речи.",
    "DISCOVERY_FAILED": "Notion недоступен.",
    "LLM_UNAVAILABLE": "Локальная модель недоступна. Попробуйте позже.",
    "LLM_INVALID_OUTPUT": "Не удалось разобрать запрос.",
    "INTENT_UNKNOWN": "Не понял, что нужно сделать в Notion.",
    "SEM_UNKNOWN_KEY": "Не удалось сопоставить запрос с Notion.",
    "SEM_TYPE": "Не удалось разобрать значение «{value}».",
    "SEM_UNSUPPORTED_OP": "Эта операция недоступна для «{target_name}».",
    "NOTION_4XX": "Notion отклонил операцию: {message}.",
    "NOTION_5XX": "Notion временно недоступен.",
    "UNDO_EXPIRED": "Отменить уже нельзя (прошло больше {minutes} минут).",
    "UNDO_FAILED": "Не удалось отменить: {message}.",
    "SESSION_EXPIRED": "Вопрос устарел. Повторите запрос.",
    "nothing_to_write": "Не понял, что именно изменить.",
    "item_not_found": "Не нашёл «{item_text}» в списке.",
    "INTERNAL": "Не удалось обработать сообщение.",
}

# ---- LLM-facing keys --------------------------------------------------------------------------

# Keys of the `pending` block the orchestrator adds to the request context when the next message
# is a free-text answer to the question already on screen (app/llm/context.py puts it into the
# payload verbatim). Russian because the whole context speaks Russian to the local model; these
# are the only strings in this module the user never sees. The block carries names and question
# text only — never a Notion id, never a context key.
PENDING_QUESTION = "вопрос"
PENDING_TARGET = "цель"
PENDING_TEXT = "исходный_текст"
