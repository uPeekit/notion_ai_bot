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
BTN_UNDO_ALL = "Отменить всё"

# ---- values -------------------------------------------------------------------------------

BOOL_YES = "Да"
BOOL_NO = "Нет"
# How a plan step may spell a tick box (app.conversation.steps reads the planner's own words).
BOOL_WORDS_TRUE = frozenset({"да", "true", "yes", "1", "+", "готово", "есть"})
BOOL_WORDS_FALSE = frozenset({"нет", "false", "no", "0", "-"})
FIELD_CLEARED = "очищено"  # a Written.value of None: the field was explicitly cleared (e.g.
                            # "убери магазин у молока"), not missing/undefined
UNTITLED = "(без названия)"  # title fallback for a page with no title text; shared with
                              # app.notion.props.page_title

# ---- clarification questions, keyed by Question.type (QType) --------------------------------

QUESTION: dict[QType, str] = {
    # Says what the bot understood (a misread intent is invisible otherwise) and points at
    # [Другое], so a single-candidate question is never a choice between one wrong answer and
    # throwing the message away.
    "target": "Не уверен, куда это: понял как «{intent}». Выберите вариант или нажмите "
              "«Другое» и напишите, что имели в виду.",
    "intent_confirm": "Похоже, вы хотите {intent}. Верно?",
    "item": "Какой элемент?",
    "item_not_found": "Не нашёл «{item_text}» в списке.",
    "field_required": "Какое значение указать для поля «{field_name}»?",
    "field_ambiguous": "Уточните значение поля «{field_name}»:",
    "date": "Дата «{field_name}»: {value}. Верно?",
    "field_confirm": "«{field_name}»: {value}. Верно?",
    "content_required": "Что написать?",
    "nothing_to_write": "Не понял, что именно изменить.",
    "clarify": "{question}",  # the model's own question, as it wrote it
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
    "plan": "выполнить несколько действий",
}
INTENT_UNKNOWN_LABEL = "запрос"

# ---- execution results ------------------------------------------------------------------------

DONE_CREATE_ITEM = "✅ Добавлено: {target_name} — {item_title}"
# The row was already in the table: nothing written, nothing changed in it.
ALREADY_THERE = "↩️ Уже есть в «{target_name}»: {item_title} — ничего не менял"
DONE_UPDATE = "✅ Обновлено: {target_name} — {item_title}"
DONE_CREATE_PAGE = "✅ Создано: {target_name} — {item_title}"
DONE_APPEND = "✅ Дописано: {target_name} — {item_title}"
DONE_LINK = "Открыть: {url}"

# ---- multi-step plans -------------------------------------------------------------------------

# Sent before a web search starts: it takes minutes, and silence reads as a hang.
SEARCHING_THE_WEB = "🔎 Ищу в интернете, это займёт пару минут…"

PLAN_HEADER = "🗂 План: {goal}"
PLAN_STEP = "Шаг {n}. {text}"
PLAN_DONE = "🏁 Готово — {summary} (шагов выполнено: {done} из {total})"
PLAN_STOPPED = "⏹ План остановлен: {summary} (шагов выполнено: {done} из {total})"
# A plan whose question nobody answered in time: the plan is gone, and saying so beats leaving
# the user to notice that the remaining steps never happened.
PLAN_ABANDONED = " План «{goal}» отменён, невыполненных шагов: {left}."

SEARCH_HEADER = "Нашёл:"
SEARCH_EMPTY = "Ничего не нашёл."

# ---- standalone replies -----------------------------------------------------------------------

# The inbox target is whatever the user flagged in targets.yaml, so all three of these name it
# at send time rather than baking one name into the sentence.
INBOX_SAVED = "Сохранил в «{target_name}»: {url}"
INBOX_SAVED_EXPIRED = "Вопрос устарел — сохранил сообщение в «{target_name}»."
INBOX_FAILED = "Не удалось сохранить в «{target_name}»."
# Short notes filed next to a message rescued into the inbox, so the user can tell later why it
# is there rather than in the target it was meant for (inbox.py trims them to MAX_NOTE). A page
# inbox keeps the note as a second paragraph under the text.
INBOX_NOTE = "Причина: {reason}"
INBOX_NOTE_UNANSWERED = "Остался без ответа вопрос: {question}"
CANCELLED = "Отменено — ничего не записал."
# Appended to CANCELLED only while no inbox page is flagged: that is exactly when the user has
# just thrown away a message they may have wanted kept, with no [В разное] button to offer.
CANCELLED_NO_INBOX = ("Чтобы такие сообщения можно было сохранить кнопкой «В разное», "
                      "выберите страницу для них в настройках: {admin_url}")
CANCELLED_NO_INBOX_NO_ADMIN = ("Чтобы такие сообщения можно было сохранить кнопкой «В разное», "
                               "укажите страницу для них в INBOX_TARGET_ID в .env.")
# Sent the moment a voice note arrives, before the download and `speech.transcribe` — the first
# one of a run loads (and, the very first time, downloads ~1.5 GB of) the Whisper model, which can
# take minutes during which the bot would otherwise be indistinguishable from a dead one.
VOICE_TRANSCRIBING = "Расшифровываю голосовое сообщение…"
UNDONE = "↩️ Отменено."
ENTER_VALUE = "Введите значение."  # answer to [Другое]: the next message is free text (F12→F4)
# [Другое] on a target or intent question: the next message is re-read together with the
# original one, so a short correction is enough.
ENTER_CORRECTION = "Напишите, что имели в виду — например: «добавь в TODO»."
# A delivered keyboard cannot be withdrawn, so the inbox offer can be pressed again after
# it has already done its job; the second press is refused with this instead of saving twice.
INBOX_ALREADY_SAVED = "Это сообщение уже сохранено."

# ---- errors (documentation/ERRORS.md), keyed by error code ------------------------------------

# Placeholders here are filled by app.conversation.orchestrator, which degrades to
# ERRORS["INTENT_UNKNOWN"] for any template it cannot fill — so a template may only ask for a
# value its caller actually has. The two validator-issued REJECTs (SEM_TYPE, SEM_UNSUPPORTED_OP)
# arrive without a candidate; their placeholder comes from Issue.detail. test_texts.py pins the
# placeholder set of every code against that.
ERRORS: dict[str, str] = {
    # Both stores are switched off on the admin page: there is nowhere to write.
    "NOTHING_ENABLED": "Обе стороны выключены — включите Notion или Obsidian на странице "
                        "настроек.",
    # A plan step named a place the workspace does not have; the model offered a different one,
    # and a step writes without asking, so nothing is written at all.
    "STEP_WRONG_TARGET": "Не нашёл, куда записать: «{target_name}».",
    "STT_EMPTY": "Не разобрал голосовое сообщение. Повторите или напишите текстом.",
    "STT_FAILED": "Ошибка распознавания речи.",
    "DISCOVERY_FAILED": "Notion недоступен.",
    "LLM_UNAVAILABLE": "Модель недоступна. Попробуйте позже.",
    "LLM_INVALID_OUTPUT": "Не удалось разобрать запрос.",
    "INTENT_UNKNOWN": "Не понял, что нужно сделать в Notion.",
    "SEM_UNKNOWN_KEY": "Не удалось сопоставить запрос с Notion.",
    "SEM_TYPE": "Не удалось разобрать значение поля «{field_name}».",
    "SEM_UNSUPPORTED_OP": "Эта операция недоступна для «{target_name}».",
    "NOTION_4XX": "Notion отклонил операцию: {message}.",
    "NOTION_5XX": "Notion временно недоступен.",
    "UNDO_EXPIRED": "Отменить уже нельзя (прошло больше {minutes} минут).",
    "UNDO_FAILED": "Не удалось отменить: {message}.",
    "SESSION_EXPIRED": "Вопрос устарел. Повторите запрос.",
    "nothing_to_write": "Не понял, что именно изменить.",
    "item_not_found": "Не нашёл «{item_text}» в списке.",
    "INTERNAL": "Не удалось обработать сообщение.",
    "WEB_UNAVAILABLE": "Поиск в интернете работает только с Claude (ANTHROPIC_API_KEY в .env).",
    "WEB_FAILED": "Не удалось ничего найти в интернете.",
    "PLAN_UNAVAILABLE": "Планы из нескольких шагов работают только с Claude (ANTHROPIC_API_KEY).",
    "PLAN_FAILED": "Не удалось составить план.",
}

# ---- /start, /help, /refresh, /targets (app.telegram.handlers) --------------------------------

HELP_TEXT = (
    "Я сохраняю и обновляю записи в Notion по текстовым и голосовым сообщениям.\n"
    "Просто напишите или наговорите, что нужно сделать — я найду подходящий раздел.\n\n"
    "Команды:\n"
    "/start — начать работу\n"
    "/help — это сообщение\n"
    "/undo — отменить последнее действие\n"
    "/cancel — отменить текущий вопрос\n"
    "/refresh — обновить список разделов Notion\n"
    "/targets — показать список разделов\n\n"
    "Сообщение, которое не удалось отнести ни к одному разделу, попадает в «разное» "
    "(если этот раздел настроен).\n\n"
    "Ещё умею: оформлять текст (списки, заголовки, чекбоксы), создавать страницы — внутри "
    "другой или в корне воркспейса, и искать в интернете: «найди рецепт борща и запиши в "
    "медиа», «найди визуальные референсы для скамейки из дуба в пройекты»."
)

REFRESH_DONE = "Обновлено. Разделов: {count}."

# The command menu Telegram shows behind the "/" button, published by app.main._register_commands
# via set_my_commands. Keys are command names without the slash; the order here is the order the
# menu shows. Every command registered in app.telegram.handlers.register belongs here, and
# HELP_TEXT lists the same set in prose.
COMMANDS: dict[str, str] = {
    "start": "Начать работу",
    "help": "Что умеет бот и какие есть команды",
    "undo": "Отменить последнее действие",
    "cancel": "Отменить текущий вопрос",
    "refresh": "Обновить список разделов Notion",
    "targets": "Показать список разделов",
}

TARGETS_HEADER = "Разделы Notion:"
TARGETS_NONE_YET = "Список разделов ещё не загружен. Отправьте /refresh."
# t.kind ("database"/"page") -> a Russian noun for the /targets tree line.
TARGET_KIND_LABELS: dict[str, str] = {"database": "база данных", "page": "страница"}
TARGETS_INBOX_MARKER = "(разное)"
TARGETS_HIDDEN_MARKER = "(скрыта от бота)"
# The synthetic target for a new top-level page (discovery's workspace_root).
ROOT_TARGET_NAME = "Корень воркспейса"
ROOT_TARGET_DESCRIPTION = (
    "Верхний уровень Notion: сюда создаётся новая самостоятельная страница, когда пользователь "
    "просит создать страницу в корне / на верхнем уровне / отдельно, не внутри другой страницы."
)

# ---- LLM-facing keys --------------------------------------------------------------------------

# Keys of the `pending` block the orchestrator adds to the request context when the next message
# is a free-text answer to the question already on screen (app/llm/context.py puts it into the
# payload verbatim). Russian because the whole context speaks Russian to the local model; these
# are the only strings in this module the user never sees. The block carries names and question
# text only — never a Notion id, never a context key.
PENDING_QUESTION = "вопрос"
PENDING_TARGET = "цель"
PENDING_TEXT = "исходный_текст"

# ---- Obsidian vault ---------------------------------------------------------------------------

# The vault's layout: folder and note names the user sees in Obsidian (see
# documentation/OBSIDIAN_PLAN.md). Renaming one here renames it for every note written later,
# not for notes already in the vault.
VAULT_TASKS_NOTE = "Задачи"
VAULT_ARCHIVE_NOTE = "Задачи — архив"
VAULT_AREAS_DIR = "Области"
VAULT_NOTES_DIR = "Заметки"
VAULT_BOOKS_DIR = "Книги"
VAULT_CLUB_DIR = "Кнуб"
VAULT_FILES_DIR = "Вложения"
VAULT_DAILY_DIR = "Дневник"
VAULT_HOME_NOTE = "Главная"
VAULT_NO_TAG_HEADING = "Без тэга"
VAULT_COUNTDOWN_TAG = "отсчёт"
# Frontmatter properties a note may carry its own date in (a meeting, a trip, a birthday).
VAULT_DATE_PROPS = ("date", "дата", "when", "due", "start")
# Books database status -> the name of its view in the books base.
VAULT_BOOK_VIEWS = {"Reading": "Читаю", "To read": "Хочу прочитать", "Read": "Прочитано"}
VAULT_BOOKS_ALL_VIEW = "Все"
VAULT_BOOKS_COLUMNS = {"status": "Статус", "author": "Автор", "created": "Добавлена"}
VAULT_CLUB_VIEW = "Встречи"
VAULT_CLUB_COLUMNS = {"book": "Книга", "author": "Автор", "date": "Дата",
                      "event_posted": "Анонс", "vyvody_posted": "Выводы"}
VAULT_HOME_TODAY = "Сегодня и просрочено"
VAULT_HOME_DOING = "В работе"
VAULT_HOME_SOON = "Скоро"
VAULT_HOME_COUNTDOWN = "Обратный отсчёт"
VAULT_HOME_READING = "Читаю"
# The guide note the bot's filer reads; the user edits it in Obsidian. Placeholders are the
# folder names above.
VAULT_GUIDE = """# Как бот раскладывает записи

Эту заметку читает бот. Пишите правила простыми словами — он им следует.

- Задачи — строками в [[{tasks}]], под заголовком своей области, с её тэгом.
  Срок — `📅 ГГГГ-ММ-ДД`, повтор — `🔁 every week on Monday`.
  Обратный отсчёт до срока — тэг `#{countdown}`.
- Книги — заметка на книгу в папке «{books}»: свойства `status` (To read / Reading / Read)
  и `author`.
- Встречи книжного клуба — в папке «{club}».
- Записи о прошедшем дне («сегодня …») — в дневник, папка «{daily}».
- Остальное — заметкой в «{notes}», со ссылкой на свою область.

## Области
"""
VAULT_INBOX_NOTE = "Разное"
# A question answered from the vault: the header, one line per hit, and the empty answer.
VAULT_SEARCH_HEADER = "Obsidian — нашёл:"
VAULT_SEARCH_HIT = "• {name}{line}"
VAULT_SEARCH_TASK = "• {line}"
VAULT_SEARCH_EMPTY = "Obsidian: ничего не нашёл."
# One line per write, appended to the reply: what the Obsidian side did.
VAULT_REPLY = "Obsidian: {what}"
VAULT_FAILED = "Obsidian: не записано ({error})"
VAULT_WHAT = {
    "task": "задача в «{note}»",
    "note": "заметка «{note}»",
    "append": "дописано в «{note}»",
    "update": "обновлено «{note}»",
    "log": "запись в дневнике «{note}»",
    "inbox": "в «{note}» — не понял, куда это",
}

# ---- the vault's agenda: the morning message and the "what now" answer -------------------------

VAULT_AGENDA_HEADER = "🌅 {date} — что на подходе:"
VAULT_AGENDA_OVERDUE = "🔴 Просрочено ({n}):"
VAULT_AGENDA_TODAY = "📌 Сегодня ({n}):"
VAULT_AGENDA_TOMORROW = "➡️ Завтра ({n}):"
VAULT_AGENDA_EVENTS = "📅 Встречи и даты ({n}):"
VAULT_AGENDA_ITEM = "• {text}"
# Answers to a question about a day or a range, and to "что мне сейчас делать".
VAULT_ON_DAY = "📅 {date}:"
VAULT_ON_RANGE = "📅 {start} — {end}:"
VAULT_NOW = "С чего начать:"

# ---- the bot's own name -----------------------------------------------------------------------

# Questions about the bot itself, answered without a model (app/address.py).
ABOUT_QUESTIONS = ("кто ты", "ты кто", "как тебя зовут", "твоё имя", "твое имя",
                   "что ты умеешь", "что умеешь", "чем помочь", "что ты можешь")
# The answer to those, and to being called by name with nothing else in the message.
ABOUT = ("Меня зовут {name}. Записываю в Notion и в Obsidian: задачи со сроками и повторами, "
         "заметки, книги, дневник. Отвечаю на вопросы: «что у меня в пятницу», «что сейчас "
         "делать», «что там про борщ». Утром в {digest_at} присылаю сводку. Любую запись можно "
         "отменить кнопкой.")
CALLED = "Да, слушаю."
# Words that may come before the name when someone calls out to the bot ("эй Джеф", "ну Джеф").
ADDRESS_OPENERS = ("эй", "ей", "hey", "ok", "окей", "ну", "слушай", "привет")

# ---- mail digest ------------------------------------------------------------------------------

MAIL_HEADER = "📬 Почта — {n} писем:"
MAIL_BUCKET = "\n{bucket} ({n}):"
MAIL_ITEM = "• {sender} — {summary}"
MAIL_MORE = "  …и ещё {n}"
MAIL_FAILED = "📬 Почту прочитать не удалось ({error})."
# How a reply quotes what came before it ("25.09.2026 Иван написал:"): everything from there
# down is the previous message again, so the classifier never sees it.
MAIL_QUOTE_MARKERS = ("написал", "wrote", "schrieb", "kirjutas")
