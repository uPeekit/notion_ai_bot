"""One-off migration of the Notion workspace into an Obsidian vault, restructured.

Only *reads* Notion. `plan()` turns the workspace into a VaultPlan — every file's path and
text, plus the files to download — without touching the disk; `apply()` writes it. A
manifest in the vault remembers what the import wrote and the hash it wrote, so a re-run
refreshes its own untouched files and never overwrites one the user has since edited, or
any file it did not create.

The shape (names in app/texts.py, VAULT_*; see documentation/OBSIDIAN_PLAN.md §5):
  tasks note + archive note     the TODO database as Tasks-plugin lines, grouped by area
  areas folder                  area pages; their embedded TODO views become Tasks queries
  notes folder                  every other page
  books folder + books base     the Books database, one note per book
  club folder + club base       the club's events
  files folder                  downloaded images and files
  home note, _bot.md            a start page, and the guide the bot's filer will read
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

import yaml

from app import texts
from app.notion import props as notion_props
from app.notion.provider import NotionProvider
from app.notion.to_markdown import CHILDREN, Renderer
from app.vault.frontmatter import render
from app.vault.names import safe_name, unique

log = logging.getLogger(__name__)

TASKS_NOTE = texts.VAULT_TASKS_NOTE
ARCHIVE_NOTE = texts.VAULT_ARCHIVE_NOTE
AREAS_DIR = texts.VAULT_AREAS_DIR
NOTES_DIR = texts.VAULT_NOTES_DIR
BOOKS_DIR = texts.VAULT_BOOKS_DIR
CLUB_DIR = texts.VAULT_CLUB_DIR
FILES_DIR = texts.VAULT_FILES_DIR
DAILY_DIR = texts.VAULT_DAILY_DIR
HOME_NOTE = texts.VAULT_HOME_NOTE
GUIDE_NOTE = "_bot"
MANIFEST = ".notion-import.json"

# Tasks-plugin status characters. Only " " and "x" are standard; "/" (Doing) and "-" (Pass)
# are custom statuses, which the user registers in one click — Tasks settings -> Task Statuses
# -> "Add All Unknown Status Types". Until then the plugin reads them as not-done, which is
# wrong only for Pass, and the home note's "in progress" list stays empty.
STATUS_CHAR = {"To do": " ", "Doing": "/", "Done": "x", "Pass": "-"}
CLOSED = ("Done", "Pass")
REPEAT_RULE = {"Daily": "every day", "Weekly": "every week", "Monthly": "every month",
               "Yearly": "every year"}
WEEKDAY = {"Mon": "Monday", "Tue": "Tuesday", "Wed": "Wednesday", "Thu": "Thursday",
           "Fri": "Friday", "Sat": "Saturday", "Sun": "Sunday"}
FILE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".pdf", ".mp4",
             ".mov", ".mp3", ".m4a", ".txt", ".docx", ".xlsx", ".zip"}
DEFAULT_EXT = {"image": ".png", "video": ".mp4", "audio": ".mp3", "pdf": ".pdf"}


# ---- configuration -----------------------------------------------------------------------

@dataclass
class Area:
    tag: str | None = None
    description: str = ""


@dataclass
class ImportConfig:
    areas: dict[str, Area] = field(default_factory=dict)  # Notion page title -> area
    skip_pages: list[str] = field(default_factory=list)  # never migrated, nor their children
    skip_databases: list[str] = field(default_factory=list)
    tasks_db: str = "TODO"
    books_db: str = "Books"
    club_db: str = "knub"
    # An embedded database view, by its title or the tail of its block id, and what replaces
    # it: "tasks" (the area's own tasks), "tasks:#tag", "base:<Name>#<view>", or "none".
    views: dict[str, str] = field(default_factory=dict)
    countdown_tag: str = texts.VAULT_COUNTDOWN_TAG

    @classmethod
    def load(cls, path: Path) -> ImportConfig:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        areas = {name: Area(**(spec or {})) for name, spec in (raw.pop("areas", {}) or {}).items()}
        return cls(areas=areas, **raw)


# ---- the plan ----------------------------------------------------------------------------

@dataclass
class VaultPlan:
    files: dict[str, str] = field(default_factory=dict)  # vault-relative path -> text
    downloads: dict[str, str] = field(default_factory=dict)  # vault-relative path -> URL
    report: list[str] = field(default_factory=list)


def _title(obj: dict) -> str:
    if obj.get("object") in ("data_source", "database"):
        return "".join(t.get("plain_text", "") for t in obj.get("title", []))
    return notion_props.page_title(obj)


def _prop(row: dict, name: str) -> Any:
    """A row property's plain value: text, option name(s), date start, bool, URL, files."""
    p = row.get("properties", {}).get(name)
    if not p:
        return None
    kind = p.get("type")
    v = p.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(t.get("plain_text", "") for t in v or []).strip()
    if kind in ("select", "status"):
        return (v or {}).get("name")
    if kind == "multi_select":
        return [o["name"] for o in v or []]
    if kind == "date":
        return (v or {}).get("start")
    if kind == "files":
        return [(f.get(f.get("type", ""), {}) or {}).get("url", "") for f in v or []]
    return v


def _day(value: str | None) -> str | None:
    return value[:10] if value else None


class NotionImporter:
    def __init__(self, provider: NotionProvider, config: ImportConfig) -> None:
        self._p = provider
        self._c = config
        self._taken: set[str] = set()
        self._note_of: dict[str, str] = {}  # Notion page id -> note name
        self._file_count: dict[str, int] = {}
        self._book_note: dict[str, str] = {}  # casefolded book title -> note name
        self._area_note: dict[str, str] = {}  # area page title -> note name
        self.plan_ = VaultPlan()

    # ---- reading ---------------------------------------------------------------------

    async def _tree(self, block_id: str) -> list[dict]:
        blocks = await self._p.block_children(block_id, limit=1000)
        for b in blocks:
            if b.get("has_children") and b["type"] not in ("child_page", "child_database"):
                b[CHILDREN] = await self._tree(b["id"])
        return blocks

    # ---- naming ----------------------------------------------------------------------

    def _name(self, notion_id: str | None, title: str) -> str:
        name = unique(safe_name(title), self._taken)
        if notion_id:
            self._note_of[notion_id] = name
        return name

    def _file_name(self, note: str) -> Callable[[str, str], str]:
        def name(url: str, kind: str) -> str:
            ext = PurePosixPath(urlsplit(url).path).suffix.lower()
            if ext not in FILE_EXTS:
                ext = DEFAULT_EXT.get(kind, ".bin")
            n = self._file_count.get(note, 0) + 1
            self._file_count[note] = n
            file = unique(f"{note} {n}", self._taken) + ext
            self.plan_.downloads[f"{FILES_DIR}/{file}"] = url
            return file
        return name

    def _renderer(self, note: str, area: Area | None = None) -> Renderer:
        return Renderer(page_note=self._note_of.get, file_name=self._file_name(note),
                        view=lambda b: self._view(b, area))

    def _view(self, block: dict, area: Area | None) -> str:
        title = block.get("child_database", {}).get("title", "")
        spec = self._c.views.get(title)
        if spec is None:
            spec = next((v for k, v in self._c.views.items()
                         if len(k) >= 6 and block.get("id", "").replace("-", "").endswith(k)),
                        None)
        if spec is None:
            spec = "tasks" if area is not None and area.tag else "none"
        if spec == "none":
            return ""
        if spec.startswith("base:"):
            base, _, view = spec[5:].partition("#")
            return f"![[{base}.base#{view}]]" if view else f"![[{base}.base]]"
        tag = spec[6:] if spec.startswith("tasks:") else f"#{area.tag}" if area and area.tag else ""
        if not tag:
            return ""
        return "\n".join(["```tasks", "not done", f"tags include {tag}", "sort by due",
                          "```"])

    # ---- the whole workspace ---------------------------------------------------------

    async def plan(self) -> VaultPlan:
        pages = [p for p in await self._p.search(object_type="page")
                 if not (p.get("archived") or p.get("in_trash"))]
        sources = {_title(d): d for d in await self._p.search(object_type="data_source")}
        by_id = {p["id"]: p for p in pages}
        rows = {name: await self._p.query_all(d["id"]) for name, d in sources.items()
                if name not in self._c.skip_databases}
        for name in self._c.skip_databases:
            if name in sources:
                self.plan_.report.append(f"skipped database «{name}»")

        loose = [p for p in pages if p.get("parent", {}).get("type") != "data_source_id"]
        kept = [p for p in loose if not self._skipped(p, by_id)]
        for p in loose:
            if p not in kept:
                self.plan_.report.append(f"skipped page «{_title(p)}»")

        # Names first, so a link from any page to any other resolves whatever the order.
        areas = [p for p in kept if _title(p) in self._c.areas]
        others = [p for p in kept if p not in areas]
        for p in areas:
            self._area_note[_title(p)] = self._name(p["id"], _title(p))
        for p in others:
            self._name(p["id"], _title(p))
        books = rows.get(self._c.books_db, [])
        for r in books:
            self._book_note[_title(r).casefold()] = self._name(r["id"], _title(r))
        club = rows.get(self._c.club_db, [])
        for r in club:
            self._name(r["id"], " ".join(x for x in (_day(_prop(r, "Date")),
                                                     _title(r)) if x))

        for p in areas:
            await self._area(p)
        for p in others:
            await self._page(p, by_id)
        await self._books(books)
        await self._club(club)
        await self._tasks(rows.get(self._c.tasks_db, []))
        self._home()
        self._guide()
        return self.plan_

    def _skipped(self, page: dict, by_id: dict[str, dict]) -> bool:
        """A skipped page, or anything below one (a secrets page's children are secrets too)."""
        seen: set[str] = set()
        while page is not None and page["id"] not in seen:
            seen.add(page["id"])
            if _title(page) in self._c.skip_pages:
                return True
            page = by_id.get(page.get("parent", {}).get("page_id", ""))
        return False

    # ---- pages -----------------------------------------------------------------------

    async def _area(self, page: dict) -> None:
        name = self._note_of[page["id"]]
        area = self._c.areas[_title(page)]
        r = self._renderer(name, area)
        body = r.render(await self._tree(page["id"]))
        self._add(f"{AREAS_DIR}/{name}.md",
                  render({"tag": area.tag, "notion": page.get("url")}, body), r)

    async def _page(self, page: dict, by_id: dict[str, dict]) -> None:
        name = self._note_of[page["id"]]
        parent = self._note_of.get(page.get("parent", {}).get("page_id", ""))
        r = self._renderer(name)
        body = r.render(await self._tree(page["id"]))
        self._add(f"{NOTES_DIR}/{name}.md", render(
            {"parent": f"[[{parent}]]" if parent else None, "notion": page.get("url")}, body), r)

    def _add(self, path: str, text: str, r: Renderer | None = None) -> None:
        self.plan_.files[path] = text
        if r is not None and r.notes:
            self.plan_.report.append(f"{path}: not carried over: {', '.join(sorted(set(r.notes)))}")

    # ---- databases -------------------------------------------------------------------

    async def _books(self, rows: list[dict]) -> None:
        for row in rows:
            name = self._note_of[row["id"]]
            author = _prop(row, "author")
            r = self._renderer(name)
            body = r.render(await self._tree(row["id"]))
            self._add(f"{BOOKS_DIR}/{name}.md", render({
                "status": _prop(row, "Status"),
                "author": f"[[{safe_name(author)}]]" if author else None,
                "created": _day(row.get("created_time")),
                "notion": row.get("url"),
            }, body), r)
        if rows:
            self._add(f"{BOOKS_DIR}.base", _books_base())

    async def _club(self, rows: list[dict]) -> None:
        for row in rows:
            name = self._note_of[row["id"]]
            book = _title(row)
            r = self._renderer(name)
            images = [f"![[{r.file_name(u, 'image')}]]" for u in _prop(row, "image") or [] if u]
            body = "\n\n".join(x for x in ("\n".join(images),
                                           r.render(await self._tree(row["id"]))) if x)
            linked = self._book_note.get(book.casefold())
            self._add(f"{CLUB_DIR}/{name}.md", render({
                "book": f"[[{linked}]]" if linked else book,
                "author": _prop(row, "author"),
                "date": _day(_prop(row, "Date")),
                "url": _prop(row, "URL"),
                "event_posted": _prop(row, "event_posted"),
                "vyvody_posted": _prop(row, "vyvody_posted"),
                "notion": row.get("url"),
            }, body), r)
        if rows:
            self._add(f"{CLUB_DIR}.base", _club_base())

    async def _tasks(self, rows: list[dict]) -> None:
        tag_order = [a.tag for a in self._c.areas.values() if a.tag]
        heading = {a.tag: title for title, a in self._c.areas.items() if a.tag}
        open_: dict[str | None, list[str]] = {}
        closed: list[tuple[str, list[str]]] = []
        timed = undated_repeat = 0
        for row in sorted(rows, key=lambda r: (_prop(r, "Due") or "9999", _title(r).casefold())):
            status = _prop(row, "Status") or "To do"
            tags = _prop(row, "Tags") or []
            due = _prop(row, "Due")
            timed += bool(due and len(due) > 10)
            rule = _rule(_prop(row, "Repeat"), _prop(row, "Repeat Days") or [])
            undated_repeat += bool(rule and not due)
            words = [_title(row) or "?"] + [f"#{t}" for t in tags]
            if _prop(row, "Trackable"):
                words.append(f"#{self._c.countdown_tag}")
            if rule:
                words.append(f"🔁 {rule}")
            if due:
                words.append(f"📅 {due[:10]}")
            line = f"- [{STATUS_CHAR.get(status, ' ')}] " + " ".join(words)
            r = self._renderer(TASKS_NOTE)
            body = r.render(await self._tree(row["id"]))
            lines = [line] + [f"\t{x}" for x in body.splitlines() if x.strip()]
            if status in CLOSED:
                closed.append((row.get("created_time", ""), lines))
            else:
                group = next((t for t in tag_order if t in tags), None)
                open_.setdefault(group, []).extend(lines)
        sections = []
        for tag in [*tag_order, None]:
            if open_.get(tag):
                title = heading[tag] if tag else texts.VAULT_NO_TAG_HEADING
                sections.append(f"## {title}\n\n" + "\n".join(open_[tag]))
        self._add(f"{TASKS_NOTE}.md", "\n\n".join(sections) + "\n")
        closed.sort(key=lambda c: c[0], reverse=True)
        self._add(f"{ARCHIVE_NOTE}.md", "\n".join(x for _, ls in closed for x in ls) + "\n")
        self.plan_.report.append(
            f"tasks: {sum(len(v) for v in open_.values())} open lines, {len(closed)} closed")
        if timed:
            self.plan_.report.append(f"tasks: {timed} due times dropped (Tasks keeps dates only)")
        if undated_repeat:
            self.plan_.report.append(
                f"tasks: {undated_repeat} repeating tasks have no due date — set one in Obsidian "
                "or the repeat has nothing to count from")

    # ---- generated notes -------------------------------------------------------------

    def _home(self) -> None:
        def query(*lines: str) -> str:
            return "\n".join(["```tasks", *lines, "```"])
        parts = [
            f"## {texts.VAULT_HOME_TODAY}",
            query("not done", "due before tomorrow", "sort by due"),
            f"## {texts.VAULT_HOME_DOING}",
            query("not done", "status.type is IN_PROGRESS"),
            f"## {texts.VAULT_HOME_SOON}",
            query("not done", "due after today", "sort by due", "limit 15"),
            f"## {texts.VAULT_HOME_COUNTDOWN}",
            query("not done", f"tags include #{self._c.countdown_tag}", "sort by due"),
        ]
        if f"{BOOKS_DIR}.base" in self.plan_.files:
            reading = texts.VAULT_BOOK_VIEWS["Reading"]
            parts += [f"## {texts.VAULT_HOME_READING}", f"![[{BOOKS_DIR}.base#{reading}]]"]
        self._add(f"{HOME_NOTE}.md", "\n\n".join(parts) + "\n")

    def _guide(self) -> None:
        lines = [texts.VAULT_GUIDE.format(
            tasks=TASKS_NOTE, countdown=self._c.countdown_tag, books=BOOKS_DIR, club=CLUB_DIR,
            daily=DAILY_DIR, notes=NOTES_DIR)]
        for title, area in self._c.areas.items():
            name = self._area_note.get(title, safe_name(title))
            tag = f" `#{area.tag}`" if area.tag else ""
            desc = f" — {area.description}" if area.description else ""
            lines.append(f"- [[{name}]]{tag}{desc}")
        self._add(f"{GUIDE_NOTE}.md", "\n".join(lines) + "\n")


def _rule(repeat: str | None, days: list[str]) -> str | None:
    if days:
        return "every week on " + ", ".join(WEEKDAY.get(d, d) for d in days)
    return REPEAT_RULE.get(repeat or "")


def _books_base() -> str:
    views: list[dict] = [{
        "type": "table", "name": texts.VAULT_BOOKS_ALL_VIEW,
        "groupBy": {"property": "note.status", "direction": "ASC"},
        "order": ["file.name", "note.author", "note.status", "note.created"],
    }]
    for status, view in texts.VAULT_BOOK_VIEWS.items():
        views.append({"type": "table", "name": view,
                      "filters": {"and": [f'status == "{status}"']},
                      "order": ["file.name", "note.author", "note.created"]})
    return _base(BOOKS_DIR, views, texts.VAULT_BOOKS_COLUMNS)


def _club_base() -> str:
    return _base(CLUB_DIR, [{
        "type": "table", "name": texts.VAULT_CLUB_VIEW,
        "order": ["file.name", "note.book", "note.author", "note.date", "note.event_posted",
                  "note.vyvody_posted"],
    }], texts.VAULT_CLUB_COLUMNS)


def _base(folder: str, views: list[dict], names: dict[str, str]) -> str:
    return yaml.safe_dump({
        "filters": {"and": [f'file.inFolder("{folder}")', 'file.ext == "md"']},
        "properties": {k: {"displayName": v} for k, v in names.items()},
        "views": views,
    }, allow_unicode=True, sort_keys=False, width=1000)


# ---- writing -----------------------------------------------------------------------------

@dataclass
class ApplyReport:
    written: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    kept_edited: list[str] = field(default_factory=list)  # the user changed it since the import
    kept_foreign: list[str] = field(default_factory=list)  # not the import's file
    failed: list[str] = field(default_factory=list)


def _hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _inside(vault: Path, rel: str) -> Path:
    path = (vault / rel).resolve()
    if not path.is_relative_to(vault.resolve()) or ".obsidian" in PurePosixPath(rel).parts:
        raise ValueError(f"refusing to write outside the vault's notes: {rel}")
    return path


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


async def apply(plan: VaultPlan, vault: Path,
                download: Callable[[str], Awaitable[bytes]]) -> ApplyReport:
    """Write the plan into `vault`. A file is (re)written only when it is new, or when it is
    the import's own and still exactly as the import left it."""
    manifest_path = vault / MANIFEST
    manifest: dict[str, str] = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")).get("files", {})
    report = ApplyReport()

    async def put(rel: str, data: bytes) -> None:
        path = _inside(vault, rel)
        if path.exists():
            current = _hash(path.read_bytes())
            if current == _hash(data):
                report.unchanged.append(rel)
                manifest[rel] = current
                return
            if rel not in manifest:
                report.kept_foreign.append(rel)
                return
            if manifest[rel] != current:
                report.kept_edited.append(rel)
                return
        _write(path, data)
        manifest[rel] = _hash(data)
        report.written.append(rel)

    for rel, text in plan.files.items():
        await put(rel, text.encode("utf-8"))
    for rel, url in plan.downloads.items():
        path = _inside(vault, rel)
        if path.exists() and rel in manifest:
            report.unchanged.append(rel)
            continue
        try:
            await put(rel, await download(url))
        except Exception as e:  # one broken image must not stop the migration
            log.warning("download failed for %s: %s", rel, type(e).__name__)
            report.failed.append(rel)
    _write(manifest_path, json.dumps({"files": manifest}, ensure_ascii=False,
                                     indent=1).encode("utf-8"))
    return report
