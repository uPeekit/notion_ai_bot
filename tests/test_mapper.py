from app.commands.models import CreateItem, CreatePage, PropertyWrite, Search
from app.notion.mapper import (
    RICH_TEXT_LIMIT,
    create_item_payload,
    create_page_payload,
    paragraph_blocks,
    properties_payload,
    property_payload,
    read_to_write,
    search_filter,
)


def pw(pid, name, type, value):
    return PropertyWrite(property_id=pid, property_name=name, type=type, value=value)


def test_property_payloads():
    assert property_payload(pw("t", "Название", "title", "Молоко")) == {
        "title": [{"type": "text", "text": {"content": "Молоко"}}]}
    assert property_payload(pw("n", "Заметка", "rich_text", "x")) == {
        "rich_text": [{"type": "text", "text": {"content": "x"}}]}
    assert property_payload(pw("n", "Заметка", "rich_text", None)) == {"rich_text": []}
    assert property_payload(pw("s", "Магазин", "select", {"id": "o1", "name": "Rimi"})) == {
        "select": {"id": "o1"}}
    assert property_payload(pw("s", "Магазин", "select", None)) == {"select": None}
    assert property_payload(pw("s", "Статус", "status", {"id": "o3", "name": "Done"})) == {
        "status": {"id": "o3"}}
    assert property_payload(pw("s", "Статус", "status", None)) is None
    assert property_payload(pw("m", "Теги", "multi_select", [{"id": "a", "name": "x"}])) == {
        "multi_select": [{"id": "a"}]}
    assert property_payload(pw("m", "Теги", "multi_select", None)) == {"multi_select": []}
    assert property_payload(pw("r", "Проект", "relation", [{"id": "p1", "name": "Дом"}])) == {
        "relation": [{"id": "p1"}]}
    assert property_payload(pw("d", "Срок", "date", {"start": "2026-09-11", "end": None})) == {
        "date": {"start": "2026-09-11", "end": None}}
    assert property_payload(pw("d", "Срок", "date", None)) == {"date": None}
    assert property_payload(pw("c", "Куплено", "checkbox", True)) == {"checkbox": True}
    assert property_payload(pw("c", "Куплено", "checkbox", None)) == {"checkbox": False}
    assert property_payload(pw("q", "Количество", "number", 2)) == {"number": 2}
    assert property_payload(pw("u", "Ссылка", "url", "https://x")) == {"url": "https://x"}
    assert property_payload(pw("u", "Ссылка", "url", None)) == {"url": None}


def test_property_payload_malformed_value_returns_none():
    assert property_payload(pw("s", "Магазин", "select", "Rimi")) is None
    assert property_payload(pw("d", "Срок", "date", "2026-09-11")) is None


def test_properties_payload_keys_by_id_and_skips_none():
    props = [pw("s", "Статус", "status", None), pw("t", "Название", "title", "x")]
    assert list(properties_payload(props)) == ["t"]


def test_create_item_and_page_payloads():
    cmd = CreateItem(data_source_id="ds", target_name="Покупки",
                     properties=[pw("t", "Название", "title", "x")])
    parent, props = create_item_payload(cmd)
    assert parent == {"type": "data_source_id", "data_source_id": "ds"} and "t" in props
    page = CreatePage(parent_page_id="pg", target_name="Идеи", title="Отпуск", body=["a", "b"])
    parent, props, children = create_page_payload(page)
    assert parent == {"type": "page_id", "page_id": "pg"}
    assert props == {"title": {"title": [{"type": "text", "text": {"content": "Отпуск"}}]}}
    assert [c["paragraph"]["rich_text"][0]["text"]["content"] for c in children] == ["a", "b"]


def test_paragraph_blocks_split_long_text():
    blocks = paragraph_blocks(["x" * (RICH_TEXT_LIMIT + 5)])
    assert len(blocks) == 1 and len(blocks[0]["paragraph"]["rich_text"]) == 2
    assert blocks[0]["object"] == "block" and blocks[0]["type"] == "paragraph"
    assert paragraph_blocks([]) == []


def test_search_filter_title_only_unchanged_shape():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название",
                query="Rimi")
    assert search_filter(cmd) == {"property": "Название", "title": {"contains": "Rimi"}}
    assert search_filter(
        Search(data_source_id=None, target_name="Идеи", title_property=None, query="x")
    ) is None


def test_search_filter_no_conditions_is_none():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название", query="")
    assert search_filter(cmd) is None


def test_search_filter_select_only():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название", query="",
                filters=[pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"})])
    assert search_filter(cmd) == {"property": "Магазин", "select": {"equals": "Rimi"}}


def test_search_filter_select_and_title_combine_with_and():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название",
                query="Rimi",
                filters=[pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"})])
    assert search_filter(cmd) == {"and": [
        {"property": "Магазин", "select": {"equals": "Rimi"}},
        {"property": "Название", "title": {"contains": "Rimi"}},
    ]}


def test_search_filter_multi_select_two_values():
    cmd = Search(data_source_id="ds", target_name="Задачи", title_property="Задача", query="",
                filters=[pw("tags", "Теги", "multi_select",
                            [{"id": "a", "name": "дом"}, {"id": "b", "name": "работа"}])])
    assert search_filter(cmd) == {"and": [
        {"property": "Теги", "multi_select": {"contains": "дом"}},
        {"property": "Теги", "multi_select": {"contains": "работа"}},
    ]}


def test_search_filter_relation_uses_page_id():
    cmd = Search(data_source_id="ds", target_name="Задачи", title_property="Задача", query="",
                filters=[pw("project", "Проект", "relation", [{"id": "p-home", "name": "Дом"}])])
    assert search_filter(cmd) == {"property": "Проект", "relation": {"contains": "p-home"}}


def test_search_filter_malformed_filter_is_skipped_not_fatal():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название", query="",
                filters=[
                    pw("shop", "Магазин", "select", {"id": "o1", "name": "Rimi"}),
                    pw("cat", "Категория", "select", "Еда"),  # malformed: bare string, not a dict
                    pw("done", "Куплено", "checkbox", True),
                ])
    assert search_filter(cmd) == {"and": [
        {"property": "Магазин", "select": {"equals": "Rimi"}},
        {"property": "Куплено", "checkbox": {"equals": True}},
    ]}


def test_search_filter_checkbox():
    cmd = Search(data_source_id="ds", target_name="Покупки", title_property="Название", query="",
                filters=[pw("done", "Куплено", "checkbox", True)])
    assert search_filter(cmd) == {"property": "Куплено", "checkbox": {"equals": True}}


def test_read_to_write_roundtrip_shapes():
    assert read_to_write({"type": "title", "title": [{"plain_text": "Хлеб"}]}) == {
        "title": [{"type": "text", "text": {"content": "Хлеб"}}]}
    assert read_to_write({"type": "select", "select": {"id": "o1", "name": "Rimi"}}) == {
        "select": {"id": "o1"}}
    assert read_to_write({"type": "select", "select": None}) == {"select": None}
    assert read_to_write({"type": "status", "status": {"id": "o2"}}) == {"status": {"id": "o2"}}
    assert read_to_write(
        {"type": "multi_select", "multi_select": [{"id": "a"}, {"id": "b"}]}
    ) == {"multi_select": [{"id": "a"}, {"id": "b"}]}
    assert read_to_write({"type": "relation", "relation": [{"id": "p1"}]}) == {
        "relation": [{"id": "p1"}]}
    assert read_to_write({"type": "date", "date": {"start": "2026-09-11", "end": None}}) == {
        "date": {"start": "2026-09-11", "end": None}}
    assert read_to_write({"type": "checkbox", "checkbox": False}) == {"checkbox": False}
    assert read_to_write({"type": "number", "number": None}) == {"number": None}
    assert read_to_write({"type": "url", "url": "https://x"}) == {"url": "https://x"}
    assert read_to_write({"type": "formula", "formula": {}}) is None
    assert read_to_write({"type": "rich_text", "rich_text": []}) == {"rich_text": []}


def test_read_to_write_has_more_truncated_property_returns_none():
    assert read_to_write(
        {"type": "relation", "relation": [{"id": "a"}], "has_more": True}
    ) is None
    assert read_to_write({"type": "relation", "relation": [{"id": "a"}]}) == {
        "relation": [{"id": "a"}]}


def test_read_to_write_status_unset_is_skipped():
    assert read_to_write({"type": "status", "status": None}) is None
    assert read_to_write({"type": "select", "select": None}) == {"select": None}
