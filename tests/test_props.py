from app.notion.props import field_type, item_hint, page_title, plain_text


def test_plain_text_joins():
    assert plain_text([{"plain_text": "a "}, {"plain_text": "b"}]) == "a b"
    assert plain_text([]) == ""


def test_page_title_from_any_title_property():
    page = {"properties": {"Название": {"type": "title", "title": [{"plain_text": "Хлеб"}]},
                           "x": {"type": "select"}}}
    assert page_title(page) == "Хлеб"
    assert page_title({"properties": {}}) == "(без названия)"


def test_field_type_mapping():
    for t in ["title", "rich_text", "select", "multi_select", "status", "date", "checkbox",
              "number", "url", "relation"]:
        assert field_type({"type": t}) == t
    assert field_type({"type": "formula"}) == "readonly"
    assert field_type({"type": "people"}) == "readonly"


def test_item_hint_prefers_status_then_checkbox():
    page = {"properties": {
        "Статус": {"type": "status", "status": {"name": "В работе"}},
        "Готово": {"type": "checkbox", "checkbox": True},
    }}
    assert item_hint(page) == "В работе"
    page = {"properties": {"Куплено": {"type": "checkbox", "checkbox": True}}}
    assert item_hint(page) == "Куплено"
    assert item_hint({"properties": {"Куплено": {"type": "checkbox", "checkbox": False}}}) is None
