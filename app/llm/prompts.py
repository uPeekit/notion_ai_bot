"""Russian system prompt and message assembly. The JSON schema enforces the shape; the prompt
explains the semantics."""

from __future__ import annotations

import json

from app.conversation.plan import PlanState
from app.llm.context import Context
from app.notion.snapshot import Target

# The context's `pending` key under which a plan step carries its plan (goal, done steps).
PLAN_KEY = "plan"

SYSTEM_PROMPT = """Ты — модуль интерпретации команд личного ассистента для Notion. Ты ничего не \
выполняешь: \
ты разбираешь сообщение пользователя и возвращаешь JSON строго по заданной схеме.

В сообщении пользователя есть контекст (JSON): текущее время now, часовой пояс tz, день недели \
weekday и \
список целей targets. Цель — база (kind=database) или страница (kind=page). У цели есть key, \
название, \
путь, описание, поля fields (key, название, тип, обязательность, допустимые варианты options) и \
существующие элементы items (для базы) или children (для страницы): ключ → название. У поля \
может быть option_descriptions — что означает вариант, словами пользователя. Может быть и \
workspace_note — заметка пользователя о том, как устроено его пространство: следуй ей при \
выборе цели и значений.

Порядок разбора:
1. intent: create — добавить запись в базу или подстраницу; update — изменить существующий \
элемент; \
append — дописать текст на страницу; search — найти; unknown — сообщение не про Notion.
2. target: только key из targets. Если правдоподобно несколько целей — верни до трёх кандидатов, \
лучший первым, у каждого своя confidence от 0 до 1. Выбор цели меняет смысл: «купи хлеб» для \
списка \
покупок — запись «Хлеб», для задач — задача «Купить хлеб».
3. Для каждого кандидата заполни ВСЕ его поля. status поля:
   value — значение названо или однозначно следует из текста; укажи confidence и source_text \
(фрагмент \
исходного сообщения);
   ambiguous — поле упомянуто, но подходят несколько вариантов; перечисли candidates;
   explicit_null — пользователь явно просит очистить/убрать значение;
   not_mentioned — в сообщении об этом ничего нет.
4. Для update и append выбери item из items/children выбранной цели. Подходит несколько — \
item=null и \
перечисли item_candidates. Не подходит ничего — item=null и пустой item_candidates. Если ни один \
элемент не подходит, верни item=null, пустой item_candidates и item_text — фрагмент сообщения, \
которым пользователь назвал элемент.
5. Текст для append или тело новой подстраницы — в content. Поисковая фраза — в search_query.

Правила:
- Используй только ключи из контекста. Не придумывай поля, варианты и элементы.
- select/status/relation/multi_select: значение — только ключ из options. Если названный вариант \
не \
совпадает ни с одним — ambiguous (если похожи несколько) или not_mentioned.
- Вариант с описанием в option_descriptions выбирай и без названия, когда сообщение подходит под \
описание: status=value, confidence по степени уверенности. Варианты без описания не угадывай.
- Не подбирай «похожий» вариант: если названо «Селвер», а в options его нет — not_mentioned; \
если названо «Рими» и есть только Rimi — это value (транслитерация допустима).
- Даты: поле даты всегда status=value с конкретным start (YYYY-MM-DD или YYYY-MM-DDTHH:MM), \
вычисленным от now и weekday. Неуверенность выражай только через confidence (например 0.6 для \
«в понедельник» без уточнения недели), никогда через ambiguous. end указывай только для явного \
интервала.
- Для дат используй блок calendar из контекста: бери готовую дату, не считай сам. «На следующей \
неделе» без дня — start = понедельник следующей недели, confidence ≤ 0.7.
- Заголовок (title): коротко, по-русски, без командного глагола для списков предметов; для задач — \
инфинитив («Купить билеты»).
- Поле, о котором в сообщении ничего нет — not_mentioned, независимо от того, обязательное оно \
или нет. required означает лишь, что бот потом переспросит; не заполняй обязательные поля \
догадками.
- notes — короткая заметка о сомнениях, может быть пустой строкой.

Примеры (ключи t1, t2, t1.i2, t1.i4 ниже — иллюстрация формата ключей, не бери их буквально):
1. «добавь хлеб» при наличии в контексте и списка покупок, и списка задач — цель неоднозначна: \
верни двух кандидатов, лучший {"target":"t1","confidence":0.55,...} (покупки), второй \
{"target":"t2","confidence":0.45,...} (задачи); поля заполни для каждого отдельно; item у обоих \
null.
2. «отметь молоко купленным» при items «Молоко» (t1.i2) и «Молоко овсяное» (t1.i4) — товар \
неоднозначен: item=null, item_candidates=["t1.i2","t1.i4"]; действие при этом однозначно: поле \
"Куплено" status=value, value=true.
Отвечай только JSON."""


NOTES_LAST = "- notes — короткая заметка о сомнениях, может быть пустой строкой.\n"
NOTES_FIRST = (
    "- notes пиши ПЕРВЫМ, до intent: одно-два коротких предложения — что это за запрос и в "
    "какую цель он относится по её описанию (например: «покупка — это дело, значит база задач "
    "с тегом personal»). Затем выбирай intent и кандидатов согласно этому рассуждению.\n"
)
TARGET_NAME_RULE = (
    "- target_name кандидата — имя цели из контекста; выбирай его по смыслу и по описанию цели, "
    "ключ target идёт следом и должен ему соответствовать.\n"
)


MARKDOWN_RULE = (
    "- content можно оформлять Markdown: # и ## заголовки, - список, 1. нумерованный список, "
    "- [ ] задача-чекбокс, > цитата, **жирный**, *курсив*, [текст](url), ``` блок кода ```, "
    "--- разделитель, ![подпись](url картинки). Оформляй, когда пользователь просит или текст "
    "по смыслу — список, план, структура; обычную фразу пиши как есть.\n"
)
CLARIFY_RULE = (
    "- clarify — вопрос пользователю, только если сообщение противоречиво или бессмысленно так, "
    "что любое действие было бы угадыванием (например, голос распознан как «не найди картинки… "
    "к каждому шагу» — просьба с отрицанием, которое не вяжется с остальным). Коротко, на «ты», "
    "с вариантами, если они есть. Кандидатов при этом всё равно заполни как лучшую догадку. "
    "Обычная неуверенность в цели или поле — не повод: для неё есть confidence. Иначе null.\n"
)
# Part of every prompt (the baseline too): content is always written as Markdown now, and the
# model may always ask back.
SYSTEM_PROMPT = SYSTEM_PROMPT.replace(NOTES_LAST, MARKDOWN_RULE + CLARIFY_RULE + NOTES_LAST)

WEB_RULE = (
    "- web_query — если пользователь просит найти что-то в интернете (информацию, рецепт, "
    "обзор, ссылки, картинки, визуальные референсы) и записать: intent create (новая "
    "подстраница или запись) или append (дописать на страницу), цель — куда записать, "
    "web_query — что искать, коротко. Найденное бот запишет сам, content оставь пустым. "
    "intent search — только поиск по записям в Notion, не в интернете. Если в сообщении нет "
    "просьбы искать в интернете — web_query null.\n"
    "- web_media — что искать: text — только текст; text_and_images — текст и картинки "
    "(просят «с изображениями», «с референсами», «с фото»); images — только картинки "
    "(«добавь картинок», «найди референсы», «покажи, как выглядит»).\n"
)

PLAN_RULE = (
    "- intent plan — сообщение требует нескольких действий в Notion: создать страницу и "
    "наполнить её, завести несколько задач, или несколько предметов, каждый из которых — "
    "отдельная запись в базе: «добавь книги: Солярис, Дюна, Пикник на обочине» при базе книг — "
    "plan. Дописать текст (хоть списком) на страницу — одно действие append, не plan. Одно "
    "действие — даже с поиском в интернете и картинками — не plan. Для plan кандидата укажи "
    "как лучшую догадку, поля не заполняй.\n"
)
STEP_RULE = (
    "- В контексте есть plan: это сообщение — один шаг большего плана. Выполни ровно этот шаг; "
    "plan.цель и plan.сделано — чтобы понимать ссылки вроде «на эту страницу»: страница, "
    "созданная предыдущим шагом, уже есть среди целей.\n"
)

# Appended for Claude, which answers in the flat shape of app.llm.claude.flat_schema.
FLAT_FORMAT_NOTE = """

Формат ответа (упрощённый):
- item, item_text, content, search_query, web_query: пустая строка "" вместо null.
- fields — список, а не объект: {"key": ключ поля, "status": ..., "value_json": ..., \
"confidence": ..., "source_text": ...}. Поля со статусом not_mentioned можно не перечислять.
- value_json — значение в виде JSON-строки: "\\"Купить хлеб\\"", "42", "true", \
"[\\"t1.f2.o1\\"]", "{\\"start\\":\\"2026-09-12\\",\\"end\\":null}". Для ambiguous — JSON-массив \
вариантов. Для explicit_null и not_mentioned — "null"; confidence тогда 0.
- Для multi_select и relation value_json — всегда JSON-массив, даже из одного варианта."""


def system_prompt(ctx: Context) -> str:
    """SYSTEM_PROMPT adjusted to the answer shape ctx asks for (output_schema reads the same two
    switches). With both off it is SYSTEM_PROMPT exactly — the baseline the benchmark compares
    against."""
    assert NOTES_LAST in SYSTEM_PROMPT
    extra = TARGET_NAME_RULE if ctx.name_targets else ""
    if ctx.web_research:
        extra += WEB_RULE
    if ctx.planning:
        extra += PLAN_RULE
    if ctx.pending and PLAN_KEY in ctx.pending:
        extra += STEP_RULE
    notes = NOTES_FIRST if ctx.reasoning_first else NOTES_LAST
    return SYSTEM_PROMPT.replace(NOTES_LAST, notes + extra)


def build_messages(text: str, ctx: Context, *, cloud: bool = False) -> list[dict]:
    """`cloud`: the context as a cloud model may see it (`Context.cloud_payload`)."""
    user = f"Контекст:\n{ctx.json(cloud=cloud)}\n\nСообщение пользователя:\n«{text.strip()}»"
    return [{"role": "system", "content": system_prompt(ctx)}, {"role": "user", "content": user}]


RESEARCH_PROMPT = """Ты — исследовательский модуль личного ассистента. Пользователь попросил \
найти что-то в интернете; твой ответ целиком запишется в его Notion. Куда и как записать \
(создать страницу, дописать, в какой раздел) — решает и делает бот, не ты: такие слова в \
сообщении пропускай, твоё дело — только содержание.

Найди информацию веб-поиском, при необходимости открой страницы (web_fetch), и верни только \
результат в Markdown — без вступлений, вопросов и рассказа о том, как ты искал; первая строка \
ответа — сразу заголовок ##:
- по делу и компактно: заголовки ##, списки -, ссылки [текст](url);
- в конце раздел «## Источники» со ссылками на страницы, которые ты использовал;
- ничего не выдумывай: только то, что нашёл; не извиняйся и не пиши о том, чего не нашёл;
- пиши по-русски, даже если источники на другом языке, — переводи.
Картинки не ищи: их ищет отдельный модуль.

Только если сама тема поиска противоречива или непонятна настолько, что искать нечего, ответь \
ровно одной строкой: ВОПРОС: <короткий вопрос пользователю> — и больше ничего. Во всех \
остальных случаях ищи и пиши результат; в самом результате вопросов не задавай никогда."""

IMAGE_QUERIES_PROMPT = """Пользователь просит картинки — визуальные референсы. Их будут искать \
на Wikimedia Commons, где фото подписаны в основном по-английски тем, что на них видно. \
Придумай от 1 до 3 коротких поисковых запросов на английском, 1–3 слова: конкретные предметы и \
места, которые можно сфотографировать, — «Suomenlinna», «Gamla stan», «Viking Line ferry», \
«torii gate», «oak bench». Без слов о цели и публике — family, children, trip, weekend, plan, \
ideas, October: с ними ничего не находится. Слова о том, куда записать, пропускай. Ответ — \
только запросы, по одному на строку, без нумерации и пояснений."""

IMAGE_FILTER_PROMPT = """Ты отбираешь картинки для страницы Notion пользователя. Дан его запрос и \
пронумерованный список найденных картинок (подписи с Wikimedia Commons и со страниц-источников). \
Верни в keep номера тех, что действительно подходят к теме как визуальные референсы: фото мест, \
предметов, людей, о которых запрос. Отбрось сканы писем и документов, карты, схемы, гербы, \
логотипы и всё не по теме — если их не просили. Лучше меньше, но по делу; если не подходит \
ничего — пустой список."""

# The line a research call answers with instead of a result when it needs the user first.
RESEARCH_QUESTION = "ВОПРОС:"
# The heading research images are filed under, and the sources heading they go in front of.
IMAGES_HEADING = "## Изображения"
SOURCES_HEADING = "## Источники"


PLAN_PROMPT = """Ты — планировщик личного ассистента для Notion. Пользователь поставил цель, для \
которой нужно несколько действий. Разбей её на шаги.

Каждый шаг — одна самостоятельная команда боту на русском, как если бы пользователь сказал её \
сам. Бот умеет: создать запись в базе или страницу (внутри другой страницы или в корне), \
дописать текст на страницу, изменить запись, найти в интернете и записать результат (текстом, \
с картинками или только картинки), искать по Notion. В каждом шаге называй место явно: «в \
пройекты», «на страницу Поездка в Японию» — страницу, созданную предыдущим шагом, называй её \
заголовком. Список предметов — по шагу на каждый («добавь в Books книгу Солярис»). Не больше \
25 шагов, без лишних; не проси подтверждений и ничего не спрашивай.

Каждый шаг должен что-то записать в Notion: бот не умеет «просто найти и запомнить» — шаг \
«найди в интернете список X» без места, куда записать, ничего не даст. Что знаешь сам (список \
романов автора, города для маршрута) — сразу разложи по шагам. Интернет — только вместе с \
записью: «найди достопримечательности Киото с фото и допиши на страницу Поездка в Японию».

goal — одна фраза: каким будет результат, когда цель достигнута. steps — команды по порядку."""

PLAN_NEXT_PROMPT = """Ты следишь за выполнением плана личного ассистента для Notion. Даны цель, \
исходный план и уже выполненные шаги с ответами бота (в том числе неудачи).

Реши, что дальше. Цель достигнута — done=true и summary: одна-две фразы, что получилось. \
Иначе — done=false и next_step: следующая команда боту, в том же виде, что шаги плана. Обычно \
это следующий шаг плана; но учитывай результаты: не повторяй сделанное, неудавшийся шаг \
попробуй по-другому один раз, а если и это не вышло — переходи дальше. Не придумывай работу \
сверх цели. Каждый шаг должен что-то записать в Notion: шаг, который только ищет, пропусти и \
используй то, что знаешь сам."""


def plan_message(request: str, workspace: str) -> str:
    return f"{workspace}\n\nСообщение пользователя:\n«{request.strip()}»"


def progress_message(state: PlanState, workspace: str) -> str:
    progress = {
        "цель": state.goal,
        "план": state.planned,
        "сделано": [{"шаг": s.request, "итог": s.status, "ответ_бота": s.outcome}
                    for s in state.history],
        "ответы_пользователя": state.answers,
    }
    return f"{workspace}\n\n" + json.dumps(progress, ensure_ascii=False, indent=1)


def plan_context(state: PlanState) -> dict:
    """What a plan step's interpretation is told about the plan (the context's `pending`)."""
    block = {"цель": state.goal, "сделано": [s.outcome for s in state.history]}
    if state.answers:  # what the user told the plan's earlier questions: dates, ages, choices
        block["ответы_пользователя"] = state.answers
    return {PLAN_KEY: block}


def workspace_summary(targets: list[Target], note: str) -> str:
    """The workspace as the planner sees it: every place the bot can write, one line each."""
    kinds = {"database": "база", "page": "страница"}
    lines = ["Места в Notion:"]
    for t in targets:
        desc = f" — {t.description[:150]}" if t.description else ""
        lines.append(f"- {t.path} [{kinds.get(t.kind, t.kind)}]{desc}")
        for f in t.fields:  # the tags a step may use: one it invents would just be dropped
            if f.type in ("select", "multi_select", "status") and f.options:
                names = ", ".join(o.name for o in f.options[:20])
                lines.append(f"    поле «{f.name}»: {names}")
    if note:
        lines.append(f"\nЗаметка пользователя о воркспейсе: {note}")
    return "\n".join(lines)


def image_filter_message(request: str, query: str, listing: str) -> str:
    return f"{research_message(request, query)}\n\nНайденные картинки:\n{listing}"


def research_message(request: str, query: str) -> str:
    return f"Сообщение пользователя: «{request.strip()}»\nЧто найти: {query.strip()}"


def retry_message(error: str) -> str:
    return (
        "Предыдущий ответ не прошёл проверку: "
        f"{error}\nВерни исправленный JSON строго по схеме, без пояснений."
    )
