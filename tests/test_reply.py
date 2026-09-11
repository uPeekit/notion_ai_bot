"""format_execution / format_search / format_question are transport-neutral: they take the
existing Decision/Executor models and produce plain text + row-grouped buttons, with no
Telegram dependency (that arrives in Plan 3b) and no import of app.conversation.session
(that arrives in Task 2)."""

from __future__ import annotations

import re
from pathlib import Path

from app.commands.executor import ExecutionResult, SearchHit, Written
from app.commands.models import AppendBlocks, CreateItem, CreatePage, PropertyWrite, UpdateItem
from app.conversation.reply import Button, Reply, format_execution, format_question, format_search
from app.validation.policy import Question

TOKEN = "1a2b3c4d"

# Files allowed to carry literal Cyrillic text outside app/texts.py: the two pre-existing
# LLM-facing modules (system prompt, weekday names), which speak Russian to the model, not to
# the user via the conversation layer.
ALLOWED_CYRILLIC_FILES = {
    Path("app/texts.py"), Path("app/llm/prompts.py"), Path("app/llm/context.py"),
}
CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


def test_no_stray_cyrillic_outside_texts_and_llm_modules():
    root = Path(__file__).resolve().parents[1]
    offenders = []
    for path in (root / "app").rglob("*.py"):
        rel = path.relative_to(root)
        if rel in ALLOWED_CYRILLIC_FILES:
            continue
        text = path.read_text(encoding="utf-8")
        if CYRILLIC.search(text):
            offenders.append(str(rel))
    assert offenders == []


def _all_button_ids(reply: Reply) -> list[str]:
    return [b.id for row in reply.buttons for b in row]


def _assert_ids_ok(reply: Reply) -> None:
    for bid in _all_button_ids(reply):
        assert bid.isascii()
        assert len(bid.encode("ascii")) <= 48


# ---- format_execution ----------------------------------------------------------------------

def test_format_execution_create_item_three_fields():
    cmd = CreateItem(
        data_source_id="ds-buy", target_name="Покупки",
        properties=[
            PropertyWrite(property_id="title", property_name="Название", type="title",
                          value="Молоко"),
            PropertyWrite(property_id="shop", property_name="Магазин", type="select",
                          value="Rimi"),
            PropertyWrite(property_id="qty", property_name="Количество", type="number", value=2),
        ],
    )
    result = ExecutionResult(
        command=cmd, page_id="p1", url="https://notion.so/p1",
        written=[
            Written("Название", "Молоко"), Written("Магазин", "Rimi"), Written("Количество", 2),
        ],
    )
    text = format_execution(result, target_url=None)
    assert text == (
        "✅ Добавлено: Покупки — Молоко\n"
        "• Магазин: Rimi\n"
        "• Количество: 2\n"
        "Открыть: https://notion.so/p1"
    )


def test_format_execution_update_one_field():
    cmd = UpdateItem(
        page_id="p2", target_name="Покупки", item_title="Молоко",
        properties=[PropertyWrite(property_id="done", property_name="Куплено", type="checkbox",
                                  value=True)],
    )
    result = ExecutionResult(command=cmd, page_id="p2", url="https://notion.so/p2",
                             written=[Written("Куплено", True)])
    text = format_execution(result, target_url=None)
    assert text == "✅ Обновлено: Покупки — Молоко\n• Куплено: Да\nОткрыть: https://notion.so/p2"


def test_format_execution_create_page():
    cmd = CreatePage(parent_page_id="pg-ideas", target_name="Идеи", title="Отпуск 2027",
                     body=["План поездки"])
    result = ExecutionResult(command=cmd, page_id="p3", url="https://notion.so/p3",
                             written=[Written("title", "Отпуск 2027")])
    text = format_execution(result, target_url=None)
    assert text == "✅ Создано: Идеи — Отпуск 2027\nОткрыть: https://notion.so/p3"


def test_format_execution_append_falls_back_to_target_url():
    cmd = AppendBlocks(page_id="p4", target_name="Идеи", page_title="Книги",
                       paragraphs=["Абзац раз", "Абзац два"])
    result = ExecutionResult(command=cmd, page_id="p4", url=None, block_ids=["b1", "b2"],
                             written=[Written("paragraphs", ["Абзац раз", "Абзац два"])])
    text = format_execution(result, target_url="https://notion.so/p4")
    assert text == "✅ Дописано: Идеи — Книги\nОткрыть: https://notion.so/p4"


# ---- format_search ---------------------------------------------------------------------------

def test_format_search_empty():
    cmd = CreateItem(data_source_id="ds-buy", target_name="Покупки", properties=[])
    result = ExecutionResult(command=cmd, hits=[])
    assert format_search(result) == "Ничего не нашёл."


def test_format_search_three_hits():
    cmd = CreateItem(data_source_id="ds-buy", target_name="Покупки", properties=[])
    hits = [
        SearchHit("Хлеб", "https://notion.so/h1", "h1"),
        SearchHit("Молоко", "https://notion.so/h2", "h2"),
        SearchHit("Яйца", "https://notion.so/h3", "h3"),
    ]
    result = ExecutionResult(command=cmd, hits=hits)
    assert format_search(result) == (
        "Нашёл:\n"
        "1. Хлеб — https://notion.so/h1\n"
        "2. Молоко — https://notion.so/h2\n"
        "3. Яйца — https://notion.so/h3"
    )


# ---- format_question -------------------------------------------------------------------------

def test_format_question_target_two_options():
    q = Question(type="target", target_key="t2")
    reply = format_question(q, [("t2", "Покупки"), ("t3", "Задачи")], TOKEN, inbox=True)
    assert reply.buttons == [
        [Button(f"a:{TOKEN}:t2", "Покупки")],
        [Button(f"a:{TOKEN}:t3", "Задачи")],
        [Button(f"a:{TOKEN}:cancel", "Отмена"), Button(f"a:{TOKEN}:inbox", "В разное")],
    ]
    _assert_ids_ok(reply)


def test_format_question_field_required_with_options():
    q = Question(type="field_required", target_key="t3", field_key="t3.f2", field_name="Приоритет")
    options = [("t3.f2.o1", "A"), ("t3.f2.o2", "B"), ("t3.f2.o3", "C")]
    reply = format_question(q, options, TOKEN, inbox=False)
    assert reply.text == "Какое значение указать для поля «Приоритет»?"
    assert reply.buttons == [
        [Button(f"a:{TOKEN}:t3.f2.o1", "A")],
        [Button(f"a:{TOKEN}:t3.f2.o2", "B")],
        [Button(f"a:{TOKEN}:t3.f2.o3", "C")],
        [Button(f"a:{TOKEN}:cancel", "Отмена")],
    ]
    _assert_ids_ok(reply)


def test_format_question_field_required_without_options_is_free_text():
    q = Question(type="field_required", target_key="t3", field_key="t3.f6", field_name="Теги")
    reply = format_question(q, [], TOKEN, inbox=False)
    assert reply.text == "Какое значение указать для поля «Теги»?"
    assert reply.buttons == [[Button(f"a:{TOKEN}:cancel", "Отмена")]]
    _assert_ids_ok(reply)


def test_format_question_date_confirm_and_other():
    q = Question(type="date", target_key="t3", field_key="t3.f3", field_name="Срок",
                proposed={"start": "2026-09-14", "end": None, "granularity": "date"})
    reply = format_question(q, [], TOKEN, inbox=True)
    assert reply.text == "Дата «Срок»: 14.09.2026. Верно?"
    assert reply.buttons == [[
        Button(f"a:{TOKEN}:confirm", "Да"),
        Button(f"a:{TOKEN}:other", "Другое"),
        Button(f"a:{TOKEN}:cancel", "Отмена"),
        Button(f"a:{TOKEN}:inbox", "В разное"),
    ]]
    _assert_ids_ok(reply)


def test_format_question_item_not_found_offers_add_new():
    q = Question(type="item_not_found", target_key="t2", proposed="кефир")
    reply = format_question(q, [], TOKEN, inbox=True)
    assert reply.text == "Не нашёл «кефир» в списке."
    assert reply.buttons == [[
        Button(f"a:{TOKEN}:add_new", "Добавить как новое"),
        Button(f"a:{TOKEN}:cancel", "Отмена"),
        Button(f"a:{TOKEN}:inbox", "В разное"),
    ]]
    _assert_ids_ok(reply)


def test_format_question_content_required():
    q = Question(type="content_required", target_key="t5")
    reply = format_question(q, [], TOKEN, inbox=False)
    assert reply.text == "Что написать?"
    assert reply.buttons == [[Button(f"a:{TOKEN}:cancel", "Отмена")]]
    _assert_ids_ok(reply)
