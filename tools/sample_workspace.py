"""Fixed workspace used by unit tests and the LLM benchmark."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from app.notion.snapshot import (
    DB_OPERATIONS,
    PAGE_OPERATIONS,
    Field,
    Item,
    Option,
    Target,
    WorkspaceSnapshot,
)

TZ = ZoneInfo("Europe/Tallinn")
SAMPLE_NOW = datetime(2026, 9, 9, 18, 0, tzinfo=TZ)  # Wednesday


def _opts(*names: str) -> list[Option]:
    return [Option(f"o-{n}", n) for n in names]


def _item(id: str, title: str, hint: str | None = None) -> Item:
    return Item(
        id=id, title=title, hint=hint, last_edited=SAMPLE_NOW, url=f"https://notion.so/{id}"
    )


def _f(
    id: str,
    name: str,
    type: str,
    *,
    required: bool = False,
    options: list[Option] | None = None,
    relation: str | None = None,
    description: str = "",
) -> Field:
    return Field(
        id=id,
        name=name,
        type=type,
        required=required,
        options=options or [],
        relation_data_source_id=relation,
        description=description,
    )


def sample_snapshot() -> WorkspaceSnapshot:
    projects = Target(
        id="ds-projects", kind="database", name="Проекты", path="Дом / Проекты", description="",
        parent_page_id="pg-home", database_id="db-projects",
        fields=[_f("title", "Название", "title", required=True)],
        items=[_item("p-home", "Дом"), _item("p-work", "Работа")],
        operations=DB_OPERATIONS, url="https://notion.so/ds-projects",
    )
    buy = Target(
        id="ds-buy", kind="database", name="Покупки", path="Дом / Покупки",
        description="Список покупок. Одна строка — один товар. Название без глагола.",
        parent_page_id="pg-home", database_id="db-buy",
        fields=[
            _f("title", "Название", "title", required=True),
            _f("shop", "Магазин", "select", options=_opts("Rimi", "Prisma", "Maxima")),
            _f("cat", "Категория", "select", options=_opts("Еда", "Хозтовары", "Инструменты")),
            _f("qty", "Количество", "number"),
            _f("done", "Куплено", "checkbox"),
            _f("note", "Заметка", "rich_text"),
            _f("created", "Создано", "readonly"),
        ],
        items=[
            _item("b-bread", "Хлеб"), _item("b-milk", "Молоко", "Куплено"),
            _item("b-eggs", "Яйца"), _item("b-milk2", "Молоко овсяное"),
        ],
        operations=DB_OPERATIONS, url="https://notion.so/ds-buy",
    )
    todo = Target(
        id="ds-todo", kind="database", name="Задачи", path="Дом / Задачи",
        description="Дела и напоминания. Заголовок — что сделать.",
        parent_page_id="pg-home", database_id="db-todo",
        fields=[
            _f("title", "Задача", "title", required=True),
            _f(
                "prio", "Приоритет", "select", required=True, options=_opts("A", "B", "C"),
                description="A — срочно, B — на этой неделе, C — когда-нибудь",
            ),
            _f("due", "Срок", "date"),
            _f("status", "Статус", "status", options=_opts("To do", "Doing", "Done")),
            _f(
                "project", "Проект", "relation", relation="ds-projects",
                options=[Option("p-home", "Дом"), Option("p-work", "Работа")],
            ),
            _f("tags", "Теги", "multi_select", options=_opts("дом", "работа", "здоровье")),
            _f("link", "Ссылка", "url"),
            _f("empty_sel", "Пустой список", "select"),
        ],
        items=[
            _item("t-docs", "Подготовить документы", "To do"),
            _item("t-mom", "Позвонить маме", "Done"),
            _item("t-tickets", "Купить билеты", "Doing"),
        ],
        operations=DB_OPERATIONS, url="https://notion.so/ds-todo",
    )
    ideas = Target(
        id="pg-ideas", kind="page", name="Идеи", path="Идеи",
        description="Свободные заметки и идеи; дописывать абзацами.",
        parent_page_id=None, database_id=None, fields=[],
        items=[_item("pg-trip", "Отпуск 2027"), _item("pg-books", "Книги")],
        operations=PAGE_OPERATIONS, url="https://notion.so/pg-ideas",
    )
    home = Target(
        id="pg-home", kind="page", name="Дом", path="Дом", description="",
        parent_page_id=None, database_id=None, fields=[], items=[],
        operations=PAGE_OPERATIONS, url="https://notion.so/pg-home",
    )
    return WorkspaceSnapshot(fetched_at=SAMPLE_NOW, targets=[home, buy, todo, projects, ideas])
