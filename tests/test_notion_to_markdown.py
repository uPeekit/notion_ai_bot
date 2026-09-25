"""Notion blocks → Obsidian markdown (app/notion/to_markdown.py), plus the name and
frontmatter helpers it is used with."""

from __future__ import annotations

from app.notion.to_markdown import CHILDREN, Renderer
from app.vault import frontmatter
from app.vault.names import safe_name, unique


def rt(text: str, **annotations) -> dict:
    href = annotations.pop("href", None)
    return {"type": "text", "plain_text": text, "href": href, "annotations": annotations}


def block(kind: str, text: str = "", children=None, **extra) -> dict:
    data = {"rich_text": [rt(text)] if text else [], **extra}
    b = {"id": f"id-{kind}", "type": kind, kind: data}
    if children is not None:
        b[CHILDREN] = children
    return b


def renderer(pages=None, views=None) -> Renderer:
    files: list[tuple[str, str]] = []
    r = Renderer(page_note=(pages or {}).get,
                 file_name=lambda url, kind: files.append((url, kind)) or f"f{len(files)}.png",
                 view=lambda b: (views or {}).get(b["id"], ""))
    r.files = files  # type: ignore[attr-defined]
    return r


def test_lists_keep_together_and_nest_with_tabs():
    md = renderer().render([
        block("paragraph", "Список:"),
        block("bulleted_list_item", "один", [block("bulleted_list_item", "вложенный")]),
        block("bulleted_list_item", "два"),
        block("to_do", "сделать", checked=True),
        block("to_do", "не сделано", checked=False),
        block("numbered_list_item", "первый"),
        block("numbered_list_item", "второй"),
        block("paragraph", "конец"),
    ])
    assert md == ("Список:\n\n- один\n\t- вложенный\n- два\n- [x] сделать\n- [ ] не сделано\n"
                  "1. первый\n2. второй\n\nконец")


def test_numbering_restarts_after_another_block():
    md = renderer().render([block("numbered_list_item", "a"), block("paragraph", "x"),
                            block("numbered_list_item", "b")])
    assert md == "1. a\n\nx\n\n1. b"


def test_annotations_keep_edge_spaces_outside_the_marks():
    r = renderer()
    assert r.rich([rt("жирный ", bold=True), rt("и"), rt(" курсив", italic=True)]) == \
        "**жирный** и *курсив*"
    assert r.rich([rt("x", code=True)]) == "`x`"


def test_links_to_migrated_pages_become_wikilinks_and_the_rest_stay_links():
    r = renderer(pages={"12345678-1234-1234-1234-123456789abc": "Борщ"})
    page = "https://www.notion.so/borsch-123456781234123412341234" + "56789abc"
    assert r.rich([rt("рецепт", href=page)]) == "[[Борщ|рецепт]]"
    assert r.rich([rt("Борщ", href=page)]) == "[[Борщ]]"
    assert r.rich([rt("сайт", href="https://example.com")]) == "[сайт](https://example.com)"
    assert r.rich([rt("https://example.com", href="https://example.com")]) == \
        "https://example.com"
    mention = {"type": "mention", "plain_text": "Борщ",
               "mention": {"type": "page", "page": {"id": "12345678-1234-1234-1234-123456789abc"}}}
    assert r.rich([mention]) == "[[Борщ]]"


def test_headings_quotes_callouts_toggles_code_divider():
    md = renderer().render([
        block("heading_2", "Раздел"),
        block("quote", "цитата"),
        block("callout", "важно", icon={"type": "emoji", "emoji": "💡"}),
        block("toggle", "подробнее", [block("paragraph", "внутри")]),
        block("code", "print(1)", language="python"),
        block("divider"),
    ])
    assert md == ("## Раздел\n\n> цитата\n\n> [!note]\n> 💡 важно\n\n> [!note]- подробнее\n"
                  "> внутри\n\n```python\nprint(1)\n```\n\n---")


def test_columns_flatten_and_tables_render():
    table = {"id": "t", "type": "table", "table": {}, CHILDREN: [
        {"type": "table_row", "table_row": {"cells": [[rt(" a ")], [rt("b|c")]]}},
        {"type": "table_row", "table_row": {"cells": [[rt("1")], [rt("2")]]}},
    ]}
    md = renderer().render([
        block("column_list", children=[block("column", children=[block("paragraph", "лево")]),
                                       block("column", children=[block("paragraph", "право")])]),
        table,
    ])
    assert md == "лево\n\nправо\n\n| a | b\\|c |\n| --- | --- |\n| 1 | 2 |"


def test_files_are_named_for_download_and_views_and_pages_resolve():
    r = renderer(pages={"id-child_page": "Подстраница"}, views={"v1": "![[Книги.base]]"})
    image = {"id": "i", "type": "image", "image": {
        "type": "file", "file": {"url": "https://s3/x.jpg?sig=1"},
        "caption": [rt("подпись")]}}
    view = {"id": "v1", "type": "child_database", "child_database": {"title": "Untitled"}}
    unknown_view = {"id": "v2", "type": "child_database", "child_database": {"title": "x"}}
    child = {"id": "id-child_page", "type": "child_page", "child_page": {"title": "Подстраница"}}
    md = r.render([image, view, unknown_view, child])
    assert md == "![[f1.png]]\n*подпись*\n\n![[Книги.base]]\n\n[[Подстраница]]"
    assert r.files == [("https://s3/x.jpg?sig=1", "image")]  # type: ignore[attr-defined]


def test_link_previews_and_unknown_blocks():
    r = renderer()
    md = r.render([{"id": "l", "type": "link_preview",
                    "link_preview": {"url": "https://github.com/x"}},
                   {"id": "s", "type": "some_new_block", "some_new_block": {}}])
    assert md == "https://github.com/x\n\n%% notion: some_new_block %%"
    assert r.notes == ["some_new_block"]


# ---- names -------------------------------------------------------------------------------

def test_safe_name_keeps_words_and_drops_what_a_file_or_link_cannot_hold():
    assert safe_name("Чапаев и Пустота") == "Чапаев и Пустота"
    assert safe_name('a/b:c*?"<>|#^[x]') == "a b c x"
    assert safe_name("S.N.U.F.F.") == "S.N.U.F.F"
    assert safe_name("  ") == "Untitled"
    assert safe_name("CON") == "Untitled"
    assert len(safe_name("я" * 500)) == 120


def test_unique_ignores_case():
    taken: set[str] = set()
    assert unique("Книга", taken) == "Книга"
    assert unique("книга", taken) == "книга 2"
    assert unique("Книга", taken) == "Книга 3"


# ---- frontmatter -------------------------------------------------------------------------

def test_frontmatter_round_trip_keeps_order_and_drops_empties():
    text = frontmatter.render({"status": "Read", "author": "[[Пелевин]]", "empty": "",
                               "flag": False, "none": None}, "Текст")
    assert text == ("---\nstatus: Read\nauthor: '[[Пелевин]]'\nflag: false\n---\n\nТекст\n")
    props, body = frontmatter.split(text)
    assert props == {"status": "Read", "author": "[[Пелевин]]", "flag": False}
    assert body == "Текст\n"


def test_frontmatter_split_never_loses_text():
    assert frontmatter.split("просто текст") == ({}, "просто текст")
    broken = "---\n: : :\n  - [\n---\nтело"
    assert frontmatter.split(broken) == ({}, broken)
    assert frontmatter.split("---\nнет конца") == ({}, "---\nнет конца")
