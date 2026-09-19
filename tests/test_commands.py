from app.commands.builder import build_command, paragraphs
from app.commands.models import AppendBlocks, CreateItem, CreatePage, Search, UpdateItem
from app.validation.semantic import SemanticValidator
from tests.helpers import cand, ctx_and_snapshot, make_interp, val


def best(intent, c):
    ctx, snap = ctx_and_snapshot()
    return SemanticValidator().validate(make_interp(intent, c), ctx, snap).best


def test_paragraphs():
    assert paragraphs("a\n\n b \nc") == ["a", "b", "c"]
    assert paragraphs("   ") == [] and paragraphs(None) == [] and paragraphs("x") == ["x"]


def test_create_item_command():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t2", fields={
        "t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1"), "t2.f4": val(2), "t2.f5": val(True),
        "t2.f3": {"status": "explicit_null"}}))
    cmd = build_command(c, "create", "купи молоко")
    assert isinstance(cmd, CreateItem) and cmd.data_source_id == "ds-buy"
    assert cmd.target_name == "Покупки"
    by_name = {p.property_name: p for p in cmd.properties}
    assert by_name["Название"].value == "Молоко" and by_name["Название"].property_id == "title"
    assert by_name["Магазин"].value == {"id": "o-Rimi", "name": "Rimi"}
    assert by_name["Количество"].value == 2 and by_name["Куплено"].value is True
    assert by_name["Категория"].value is None                     # explicit_null → clear
    assert "Заметка" not in by_name                                # not_mentioned → omitted
    assert cmd.model_dump()["action"] == "create_item"


def test_update_item_command_and_date_relation_json():
    ctx, _ = ctx_and_snapshot()
    c = best("update", cand(ctx, "t3", item="t3.i1", fields={
        "t3.f3": val({"start": "2026-09-11T10:00", "end": None}), "t3.f5": val(["t3.f5.o1"]),
        "t3.f4": val("t3.f4.o3")}))
    cmd = build_command(c, "update", "")
    assert isinstance(cmd, UpdateItem) and cmd.page_id == "t-docs"
    assert cmd.item_title == "Подготовить документы"
    by_name = {p.property_name: p for p in cmd.properties}
    assert by_name["Срок"].value == {"start": "2026-09-11T10:00:00+03:00", "end": None}
    assert by_name["Проект"].value == [{"id": "p-home", "name": "Дом"}]
    assert by_name["Статус"].value == {"id": "o-Done", "name": "Done"}
    cmd.model_dump_json()  # serialisable for the audit log


def test_create_page_and_append_and_search():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(
        ctx, "t5", fields={"t5.f1": val("Отпуск 2028")}, content="план\nбюджет"))
    cmd = build_command(c, "create", "")
    assert isinstance(cmd, CreatePage) and cmd.parent_page_id == "pg-ideas"
    assert cmd.title == "Отпуск 2028"
    assert cmd.body == ["план", "бюджет"]
    c = best("append", cand(ctx, "t5", item="t5.i1", content="взять палатку"))
    cmd = build_command(c, "append", "")
    assert isinstance(cmd, AppendBlocks) and cmd.page_id == "pg-trip"
    assert cmd.page_title == "Отпуск 2027"
    c = best("append", cand(ctx, "t5", content="идея"))
    assert build_command(c, "append", "").page_id == "pg-ideas"
    c = best("search", cand(ctx, "t2", search_query="Rimi"))
    cmd = build_command(c, "search", "что в покупках на Rimi")
    assert isinstance(cmd, Search) and cmd.data_source_id == "ds-buy"
    assert cmd.title_property == "Название"
    assert cmd.query == "Rimi"
    c = best("search", cand(ctx, "t2"))
    assert build_command(c, "search", "что в покупках").query == "что в покупках"


def test_create_on_a_page_without_a_body_appends_a_line():
    """The model answers "create" for «надо посмотреть фильм Uncharted» even though the page
    target holds a list; an empty sub-page is never what that meant."""
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t5", fields={"t5.f1": val("Uncharted")}))
    cmd = build_command(c, "create", "надо посмотреть фильм Uncharted")
    assert isinstance(cmd, AppendBlocks)
    assert cmd.page_id == "pg-ideas" and cmd.page_title == "Идеи"
    assert cmd.paragraphs == ["Uncharted"]


def test_create_on_a_page_stays_a_page_when_one_is_asked_for():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t5", fields={"t5.f1": val("Отпуск 2028")}))
    for text in ("создай страницу «Отпуск 2028»", "заведи отдельную страницу под отпуск",
                 "новая страница Отпуск 2028"):
        cmd = build_command(c, "create", text)
        assert isinstance(cmd, CreatePage), text
        assert cmd.parent_page_id == "pg-ideas" and cmd.title == "Отпуск 2028"
    # the page word without a create verb before it is where the line goes, not what to make
    for text in ("добавь на страницу Отпуск 2028", "на странице идей заведи список"):
        assert isinstance(build_command(c, "create", text), AppendBlocks), text


def test_search_filters_from_validated_fields():
    ctx, _ = ctx_and_snapshot()
    c = best("search", cand(ctx, "t2", fields={"t2.f2": val("t2.f2.o1")}))
    cmd = build_command(c, "search", "что в покупках на Rimi")
    assert len(cmd.filters) == 1
    assert cmd.filters[0].property_name == "Магазин"
    assert cmd.filters[0].value == {"id": "o-Rimi", "name": "Rimi"}
    assert cmd.query == ""


def test_model_content_is_markdown_lines_kept_as_written():
    ctx, _ = ctx_and_snapshot()
    content = "## План\n\n- верстак\n```\n  отступ\n```"
    c = best("create", cand(ctx, "t5", fields={"t5.f1": val("Мастерская")}, content=content))
    cmd = build_command(c, "create", "")
    assert cmd.markdown is True
    assert cmd.body == ["## План", "", "- верстак", "```", "  отступ", "```"]
    c = best("append", cand(ctx, "t5", content="- [ ] паспорт"))
    cmd = build_command(c, "append", "")
    assert cmd.markdown is True and cmd.paragraphs == ["- [ ] паспорт"]


def test_database_row_gets_the_content_as_its_page_body():
    ctx, _ = ctx_and_snapshot()
    c = best("create", cand(ctx, "t2", fields={"t2.f1": val("Молоко")}, content="**3.2%**"))
    cmd = build_command(c, "create", "")
    assert isinstance(cmd, CreateItem) and cmd.body == ["**3.2%**"] and cmd.markdown is True
    c = best("create", cand(ctx, "t2", fields={"t2.f1": val("Хлеб")}))
    assert build_command(c, "create", "").body == []


def test_old_audit_commands_still_validate_without_the_new_fields():
    assert CreateItem.model_validate({"data_source_id": "d", "target_name": "t",
                                      "properties": []}).markdown is False
    assert AppendBlocks.model_validate({"page_id": "p", "target_name": "t", "page_title": "p",
                                        "paragraphs": ["x"]}).markdown is False
