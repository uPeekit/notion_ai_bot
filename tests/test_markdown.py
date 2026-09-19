from app.notion.markdown import MAX_RICH_ELEMENTS, RICH_TEXT_LIMIT, markdown_blocks, rich_text


def kinds(text: str) -> list[str]:
    return [b["type"] for b in markdown_blocks(text)]


def plain(block: dict) -> str:
    return "".join(r["text"]["content"] for r in block[block["type"]]["rich_text"])


def test_block_types_one_per_line():
    text = "\n".join([
        "# Заголовок", "## Раздел", "### Подраздел", "- пункт", "* ещё пункт", "1. первый",
        "2) второй", "- [ ] купить", "- [x] сделано", "> цитата", "---", "просто текст",
    ])
    assert kinds(text) == [
        "heading_1", "heading_2", "heading_3", "bulleted_list_item", "bulleted_list_item",
        "numbered_list_item", "numbered_list_item", "to_do", "to_do", "quote", "divider",
        "paragraph",
    ]


def test_todo_state_and_a_cyrillic_check_mark():
    blocks = markdown_blocks("- [ ] паспорт\n- [x] зарядка\n- [х] наушники")
    assert [b["to_do"]["checked"] for b in blocks] == [False, True, True]
    assert plain(blocks[0]) == "паспорт"


def test_blank_lines_are_dropped_and_text_kept_verbatim():
    blocks = markdown_blocks("\n\n  обычная фраза, 2*3*4 = 24  \n\n")
    assert kinds("\n\n  обычная фраза  \n") == ["paragraph"]
    assert plain(blocks[0]) == "обычная фраза, 2*3*4 = 24"


def test_code_fence_keeps_indentation_and_maps_the_language():
    blocks = markdown_blocks("```py\ndef f():\n    return 1\n```\nпосле")
    assert kinds("```py\nx\n```") == ["code"]
    code = blocks[0]["code"]
    assert code["language"] == "python"
    assert code["rich_text"][0]["text"]["content"] == "def f():\n    return 1"
    assert plain(blocks[1]) == "после"


def test_unknown_language_and_unclosed_fence_still_keep_the_text():
    blocks = markdown_blocks("```brainfuck\n+++")
    assert blocks[0]["code"]["language"] == "plain text"
    assert blocks[0]["code"]["rich_text"][0]["text"]["content"] == "+++"


def test_image_line_becomes_an_external_image_with_caption():
    [block] = markdown_blocks("![Скамейка из дуба](https://example.com/bench.jpg)")
    assert block["image"]["external"]["url"] == "https://example.com/bench.jpg"
    assert block["image"]["caption"][0]["text"]["content"] == "Скамейка из дуба"


def test_inline_formatting():
    runs = rich_text("**жирный**, *курсив*, ~~зачёркнуто~~, `код` и [ссылка](https://a.b/c)")
    styled = {r["text"]["content"]: r for r in runs}
    assert styled["жирный"]["annotations"] == {"bold": True}
    assert styled["курсив"]["annotations"] == {"italic": True}
    assert styled["зачёркнуто"]["annotations"] == {"strikethrough": True}
    assert styled["код"]["annotations"] == {"code": True}
    assert styled["ссылка"]["text"]["link"] == {"url": "https://a.b/c"}
    assert "".join(r["text"]["content"] for r in runs) == \
        "жирный, курсив, зачёркнуто, код и ссылка"


def test_bare_urls_become_links():
    runs = rich_text("см. https://example.com/x, там всё.")
    assert [r["text"] for r in runs] == [
        {"content": "см. "},
        {"content": "https://example.com/x", "link": {"url": "https://example.com/x"}},
        {"content": ", там всё."},
    ]


def test_snake_case_is_not_italic():
    assert all("annotations" not in r for r in rich_text("файл my_file_name.txt"))


def test_long_text_is_split_into_notion_sized_runs():
    runs = rich_text("я" * (RICH_TEXT_LIMIT + 5))
    assert [len(r["text"]["content"]) for r in runs] == [RICH_TEXT_LIMIT, 5]


def test_too_many_runs_fall_back_to_plain_text():
    line = " ".join(["**x**"] * (MAX_RICH_ELEMENTS + 1))
    runs = rich_text(line)
    assert len(runs) == 1 and runs[0]["text"]["content"] == line
