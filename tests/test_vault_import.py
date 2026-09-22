"""The Notion → vault migration (app/vault/notion_import.py): what it plans from a workspace,
and how it writes without overwriting anything the user touched."""

from __future__ import annotations

import json

import pytest

from app import texts
from app.vault import frontmatter
from app.vault.notion_import import (
    MANIFEST,
    Area,
    ImportConfig,
    NotionImporter,
    VaultPlan,
    _inside,
    apply,
)
from tests.fakes import FakeNotionProvider


def title_prop(text: str) -> dict:
    return {"type": "title", "title": [{"plain_text": text}]}


def page(pid: str, title: str, parent: dict, **props) -> dict:
    return {"object": "page", "id": pid, "parent": parent, "url": f"https://notion.so/{pid}",
            "created_time": "2026-09-01T10:00:00.000Z",
            "properties": {"title": title_prop(title), **props}}


def row(pid: str, ds: str, title: str, **props) -> dict:
    return page(pid, title, {"type": "data_source_id", "data_source_id": ds}, **props)


def status(name: str) -> dict:
    return {"type": "status", "status": {"name": name}}


def text(value: str) -> dict:
    return {"type": "rich_text", "rich_text": [{"plain_text": value}]}


def multi(*names: str) -> dict:
    return {"type": "multi_select", "multi_select": [{"name": n} for n in names]}


def date(start: str | None) -> dict:
    return {"type": "date", "date": {"start": start} if start else None}


def para(t: str) -> dict:
    return {"id": f"p-{t}", "type": "paragraph", "has_children": False,
            "paragraph": {"rich_text": [{"type": "text", "plain_text": t, "annotations": {}}]}}


WS = {"type": "workspace", "workspace": True}


def workspace() -> FakeNotionProvider:
    n = FakeNotionProvider()
    home = page("pg-home", "дом", WS)
    secret = page("pg-secret", "секретики", WS)
    secret_child = page("pg-secret-kid", "пароли", {"type": "page_id", "page_id": "pg-secret"})
    recipe = page("pg-recipe", "Борщ", {"type": "page_id", "page_id": "pg-home"})
    books = [row("r-b1", "ds-books", "Чапаев и Пустота", Status=status("Read"),
                 author=text("Виктор Пелевин"))]
    club = [row("r-k1", "ds-knub", "Чапаев и Пустота", Date=date("2026-09-21"),
                URL={"type": "url", "url": "https://t.me/x"},
                image={"type": "files", "files": [
                    {"type": "file", "file": {"url": "https://s3.example/img.jpg?x"}}]})]
    tasks = [
        row("r-t1", "ds-todo", "платить счета", Status=status("To do"), Tags=multi("home"),
            Due=date("2026-09-15"), Repeat={"type": "select", "select": {"name": "Monthly"}},
            **{"Repeat Days": multi(), "Trackable": {"type": "checkbox", "checkbox": True}}),
        row("r-t2", "ds-todo", "зубы", Status=status("Doing"), Tags=multi(),
            Due=date("2026-09-13T09:00:00.000+03:00"),
            **{"Repeat Days": multi("Mon", "Wed")}),
        row("r-t3", "ds-todo", "старое", Status=status("Done"), Tags=multi("home")),
        row("r-t4", "ds-todo", "отменено", Status=status("Pass"), Tags=multi("home")),
    ]
    sources = [{"object": "data_source", "id": f"ds-{key}", "title": [{"plain_text": name}]}
               for key, name in (("books", "Books"), ("knub", "knub"), ("todo", "TODO"),
                                 ("people", "People"))]
    n.search_results = [home, secret, secret_child, recipe, *books, *club, *tasks, *sources]
    n.items = {"ds-books": books, "ds-knub": club, "ds-todo": tasks,
               "ds-people": [row("r-p", "ds-people", "кто-то")]}
    n.page_blocks = {
        "pg-home": [
            {"id": "view-1", "type": "child_database", "has_children": False,
             "child_database": {"title": "Untitled"}},
            {"id": "pg-recipe", "type": "child_page", "has_children": False,
             "child_page": {"title": "Борщ"}},
        ],
        "pg-recipe": [para("свекла")],
        "pg-secret-kid": [para("hunter2")],
        "r-t1": [para("квитанции в ящике")],
    }
    return n


def config() -> ImportConfig:
    return ImportConfig(areas={"дом": Area(tag="home", description="домашние дела")},
                        skip_pages=["секретики"], skip_databases=["People"])


async def test_plan_restructures_the_workspace():
    n = workspace()
    plan = await NotionImporter(n, config()).plan()
    f = plan.files
    T = texts

    area = f[f"{T.VAULT_AREAS_DIR}/дом.md"]
    assert "tags include #home" in area and "[[Борщ]]" in area

    props, body = frontmatter.split(f[f"{T.VAULT_NOTES_DIR}/Борщ.md"])
    assert props["parent"] == "[[дом]]" and body.strip() == "свекла"

    props, _ = frontmatter.split(f[f"{T.VAULT_BOOKS_DIR}/Чапаев и Пустота.md"])
    assert props == {"status": "Read", "author": "[[Виктор Пелевин]]", "created": "2026-09-01",
                     "notion": "https://notion.so/r-b1"}
    assert f"{T.VAULT_BOOKS_DIR}.base" in f

    # the club event is named by date + book, and links the book's own note
    props, body = frontmatter.split(f[f"{T.VAULT_CLUB_DIR}/2026-09-21 Чапаев и Пустота.md"])
    assert props["book"] == "[[Чапаев и Пустота]]" and props["url"] == "https://t.me/x"
    assert body.startswith("![[")
    assert list(plan.downloads.values()) == ["https://s3.example/img.jpg?x"]

    tasks = f[f"{T.VAULT_TASKS_NOTE}.md"]
    assert f"## дом\n\n- [ ] платить счета #home #{T.VAULT_COUNTDOWN_TAG} 🔁 every month " \
           f"📅 2026-09-15\n\tквитанции в ящике" in tasks
    assert f"## {T.VAULT_NO_TAG_HEADING}\n\n- [/] зубы 🔁 every week on Monday, Wednesday " \
           f"📅 2026-09-13" in tasks
    archive = f[f"{T.VAULT_ARCHIVE_NOTE}.md"]
    assert "- [x] старое #home" in archive and "- [-] отменено #home" in archive
    assert "старое" not in tasks
    assert any("1 due times dropped" in line for line in plan.report)

    assert "[[дом]] `#home` — домашние дела" in f["_bot.md"]
    assert f"{T.VAULT_HOME_NOTE}.md" in f


async def test_secrets_and_skipped_databases_never_reach_the_plan():
    n = workspace()
    plan = await NotionImporter(n, config()).plan()
    everything = json.dumps([plan.files, plan.downloads], ensure_ascii=False)
    assert "hunter2" not in everything and "пароли" not in everything
    assert "секретики" not in everything and "кто-то" not in everything
    # the secret page's child is never even read
    assert ("block_children", "pg-secret-kid") not in n.calls
    assert ("query_all", "ds-people") not in n.calls


async def test_view_overrides_by_title_and_block_id():
    n = workspace()
    cfg = config()
    cfg.views = {"view01": "base:Кнуб"}  # tail of the block id, dashes ignored
    n.page_blocks["pg-home"][0]["id"] = "aaaa-view01"
    plan = await NotionImporter(n, cfg).plan()
    area = plan.files[f"{texts.VAULT_AREAS_DIR}/дом.md"]
    assert "![[Кнуб.base]]" in area and "tags include" not in area


# ---- writing -----------------------------------------------------------------------------

async def _bytes(url: str) -> bytes:
    if "broken" in url:
        raise OSError("nope")
    return b"IMG"


async def test_apply_writes_and_never_overwrites_the_users_edits(tmp_path):
    plan = VaultPlan(files={"a.md": "one\n", "dir/b.md": "two\n"},
                     downloads={"files/x.png": "https://x/x.png", "files/y.png": "https://broken"})
    report = await apply(plan, tmp_path, _bytes)
    assert sorted(report.written) == ["a.md", "dir/b.md", "files/x.png"]
    assert report.failed == ["files/y.png"]
    assert (tmp_path / "dir/b.md").read_text(encoding="utf-8") == "two\n"
    assert (tmp_path / "files/x.png").read_bytes() == b"IMG"
    assert set(json.loads((tmp_path / MANIFEST).read_text(encoding="utf-8"))["files"]) == \
        {"a.md", "dir/b.md", "files/x.png"}

    (tmp_path / "dir/b.md").write_text("edited by me\n", encoding="utf-8")
    (tmp_path / "mine.md").write_text("my own note\n", encoding="utf-8")
    again = VaultPlan(files={"a.md": "one, refreshed\n", "dir/b.md": "two, refreshed\n",
                             "mine.md": "import's version\n"})
    report = await apply(again, tmp_path, _bytes)
    assert report.written == ["a.md"]
    assert report.kept_edited == ["dir/b.md"] and report.kept_foreign == ["mine.md"]
    assert (tmp_path / "a.md").read_text(encoding="utf-8") == "one, refreshed\n"
    assert (tmp_path / "dir/b.md").read_text(encoding="utf-8") == "edited by me\n"
    assert (tmp_path / "mine.md").read_text(encoding="utf-8") == "my own note\n"


def test_writes_stay_inside_the_vault_and_out_of_its_settings(tmp_path):
    for rel in ("../outside.md", ".obsidian/app.json", "a/../../x.md"):
        with pytest.raises(ValueError):
            _inside(tmp_path, rel)
    assert _inside(tmp_path, "Книги/x.md") == (tmp_path / "Книги/x.md").resolve()
