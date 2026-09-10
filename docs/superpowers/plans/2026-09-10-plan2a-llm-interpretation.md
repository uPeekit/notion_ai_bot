# Plan 2a: LLM Interpretation (context, schema, Ollama client, prompt, benchmark) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn a `WorkspaceSnapshot` plus user text into a validated `Interpretation` using a local Ollama model, with a benchmark that picks the model.

**Architecture:** `ContextBuilder` assigns per-request short keys (`t1`, `t1.f2`, `t1.f2.o3`, `t1.i4`) and emits a compact JSON context. `build_schema(ctx)` derives a JSON schema whose enums/consts are exactly those keys, sent to Ollama's `format` so output is grammar-constrained. `OllamaClient.interpret()` calls `/api/chat` at temperature 0, validates with the existing Pydantic `Interpretation`, retries once with the validation error. `tools/benchmark_llm.py` runs a Russian case set against several models and reports accuracy and latency.

**Tech Stack:** Python 3.12, httpx, pydantic 2, PyYAML, `jsonschema` (dev, to check schemas), Ollama (Windows install), models `qwen3:8b`, `qwen2.5:7b-instruct`, `llama3.1:8b`, `gemma3:4b`.

**Spec:** `documentation/ARCHITECTURE.md` §6, §13, §14; `documentation/DATA_MODEL.md` §3; `documentation/IMPLEMENTATION_PLAN.md` T-002, T-020…T-025.

## Global Constraints

- The LLM receives only: context JSON (keys, names, descriptions, options, item titles), time, user text. Never Notion ids, tokens, URLs.
- LLM output references targets/fields/options/items only by keys present in the context; the JSON schema enforces this with `const`/`enum`.
- Every writable field of a candidate's target appears in `fields` with one of the four statuses (`not_mentioned`, `explicit_null`, `ambiguous`, `value`).
- Page targets get one synthetic field `Заголовок` (type `title`, `field_id="__title__"`) used for sub-page creation.
- Fields of type select/status/multi_select/relation with zero options are omitted from the context (nothing valid could be chosen).
- Temperature 0; `think: false` sent only for thinking-capable model families (`qwen3`, `deepseek-r1`, `gpt-oss`, `magistral`).
- Russian prompt; dates resolved relative to the `now` in the context (Europe/Tallinn).
- `uv run ruff check .` and `uv run pytest -q` (default excludes `integration`) pass before each commit; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`; named `git add` only.
- Shell is PowerShell; uv may be at `$env:USERPROFILE\.local\bin\uv.exe`.

## File structure

```text
app/llm/__init__.py
app/llm/context.py            KeyRef, Context, ContextBuilder
app/llm/output_schema.py      build_schema(ctx)
app/llm/base.py               LLMError, LLMUnavailable, LLMInvalidOutput, LLMTrace, LLMClient protocol
app/llm/prompts.py            SYSTEM_PROMPT, build_messages(), retry_message()
app/llm/ollama.py             OllamaClient
tools/sample_workspace.py     sample_snapshot() shared by tests and benchmark
tools/benchmark_llm.py        case loading, scoring, per-model run, markdown report
tests/fixtures/ru_cases.yaml  ≥ 40 Russian cases
tests/test_context.py, tests/test_output_schema.py, tests/test_ollama.py, tests/test_prompts.py,
tests/test_benchmark.py, tests/test_llm_integration.py (marker integration)
documentation/BENCHMARK.md    results + chosen model
```

---

### Task 1: Install Ollama and pull candidate models

**Files:** none (environment). Report only.

- [ ] **Step 1:** `winget install --id Ollama.Ollama -e --accept-source-agreements --accept-package-agreements`. If winget is unavailable, download and run `https://ollama.com/download/OllamaSetup.exe`. Then open a new shell; `ollama --version` must work (the installer adds `%LOCALAPPDATA%\Programs\Ollama` to PATH; if not, use the full path).
- [ ] **Step 2:** Pull: `ollama pull qwen3:8b`, `ollama pull qwen2.5:7b-instruct`, `ollama pull llama3.1:8b`, `ollama pull gemma3:4b` (~18 GB total; run sequentially).
- [ ] **Step 3:** Verify structured output works:
```powershell
$body = '{"model":"qwen3:8b","messages":[{"role":"user","content":"Верни объект с полем city = Таллин"}],"stream":false,"think":false,"format":{"type":"object","properties":{"city":{"type":"string"}},"required":["city"]},"options":{"temperature":0}}'
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11434/api/chat -Body ([System.Text.Encoding]::UTF8.GetBytes($body)) -ContentType "application/json; charset=utf-8" | Select-Object -Expand message
```
Expected: `content` is `{"city":"Таллин"}` (or similar JSON). Record `ollama list` output and this response in the report. No commit.

---

### Task 2: Sample workspace + ContextBuilder

**Files:**
- Create: `app/llm/__init__.py` (empty), `app/llm/context.py`, `tools/sample_workspace.py`, `tests/test_context.py`

**Interfaces:**
- Consumes: `app.notion.snapshot` dataclasses.
- Produces: `KeyRef(kind, target_id, name, field_id, field_type, option_id, item_id)`; `Context(payload, keys, now, pending, fields_by_target, options_by_field, items_by_target)` with `json()`, `ref(key)`, `target_keys()`, `target_key(target_id)`, `field_keys(tk)`, `option_keys(fk)`, `item_keys(tk)`; `ContextBuilder(timezone="Europe/Tallinn", items_per_target=50).build(snapshot, now=None, pending=None) -> Context`; constants `PAGE_TITLE_FIELD_ID = "__title__"`, `RU_WEEKDAYS`, `OPTION_TYPES`. `tools.sample_workspace.sample_snapshot() -> WorkspaceSnapshot` and `SAMPLE_NOW = datetime(2026, 9, 9, 18, 0, tzinfo=ZoneInfo("Europe/Tallinn"))` (a Wednesday).

- [ ] **Step 1: `tools/sample_workspace.py`**

```python
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
    return Item(id=id, title=title, hint=hint, last_edited=SAMPLE_NOW, url=f"https://notion.so/{id}")


def _f(id: str, name: str, type: str, *, required: bool = False, options: list[Option] | None = None,
       relation: str | None = None, description: str = "") -> Field:
    return Field(id=id, name=name, type=type, required=required, options=options or [],
                 relation_data_source_id=relation, description=description)


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
        items=[_item("b-bread", "Хлеб"), _item("b-milk", "Молоко", "Куплено"), _item("b-eggs", "Яйца"),
               _item("b-milk2", "Молоко овсяное")],
        operations=DB_OPERATIONS, url="https://notion.so/ds-buy",
    )
    todo = Target(
        id="ds-todo", kind="database", name="Задачи", path="Дом / Задачи",
        description="Дела и напоминания. Заголовок — что сделать.",
        parent_page_id="pg-home", database_id="db-todo",
        fields=[
            _f("title", "Задача", "title", required=True),
            _f("prio", "Приоритет", "select", required=True, options=_opts("A", "B", "C"),
               description="A — срочно, B — на этой неделе, C — когда-нибудь"),
            _f("due", "Срок", "date"),
            _f("status", "Статус", "status", options=_opts("To do", "Doing", "Done")),
            _f("project", "Проект", "relation", relation="ds-projects",
               options=[Option("p-home", "Дом"), Option("p-work", "Работа")]),
            _f("tags", "Теги", "multi_select", options=_opts("дом", "работа", "здоровье")),
            _f("link", "Ссылка", "url"),
            _f("empty_sel", "Пустой список", "select"),
        ],
        items=[_item("t-docs", "Подготовить документы", "To do"), _item("t-mom", "Позвонить маме", "Done"),
               _item("t-tickets", "Купить билеты", "Doing")],
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
```

- [ ] **Step 2: failing tests** — `tests/test_context.py`:

```python
import json
from datetime import datetime
from zoneinfo import ZoneInfo

from app.llm.context import PAGE_TITLE_FIELD_ID, ContextBuilder
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def build(**kw):
    return ContextBuilder("Europe/Tallinn", **kw).build(sample_snapshot(), now=SAMPLE_NOW)


def test_keys_and_payload_shape():
    ctx = build()
    p = ctx.payload
    assert p["now"] == "2026-09-09T18:00+03:00"
    assert p["tz"] == "Europe/Tallinn" and p["weekday"] == "среда"
    assert [t["key"] for t in p["targets"]] == ["t1", "t2", "t3", "t4", "t5"]
    buy = p["targets"][1]
    assert buy["name"] == "Покупки" and buy["kind"] == "database" and buy["path"] == "Дом / Покупки"
    assert buy["ops"] == ["create", "search", "update"]
    names = [f["name"] for f in buy["fields"]]
    assert names == ["Название", "Магазин", "Категория", "Количество", "Куплено", "Заметка"]  # readonly dropped
    shop = buy["fields"][1]
    assert shop["key"] == "t2.f2" and shop["type"] == "select"
    assert shop["options"] == {"t2.f2.o1": "Rimi", "t2.f2.o2": "Prisma", "t2.f2.o3": "Maxima"}
    assert buy["fields"][0]["required"] is True and "required" not in shop
    assert buy["items"] == {"t2.i1": "Хлеб", "t2.i2": "Молоко (Куплено)", "t2.i3": "Яйца",
                            "t2.i4": "Молоко овсяное"}


def test_empty_option_field_dropped_and_relation_options_present():
    ctx = build()
    todo = ctx.payload["targets"][2]
    names = [f["name"] for f in todo["fields"]]
    assert "Пустой список" not in names
    project = next(f for f in todo["fields"] if f["name"] == "Проект")
    assert project["type"] == "relation" and list(project["options"].values()) == ["Дом", "Работа"]


def test_page_targets_get_synthetic_title_and_children():
    ctx = build()
    ideas = ctx.payload["targets"][4]
    assert ideas["kind"] == "page" and ideas["ops"] == ["append", "create", "search"]
    assert ideas["fields"] == [{"key": "t5.f1", "name": "Заголовок", "type": "title", "required": True,
                                "description": "Название новой подстраницы"}]
    assert ideas["children"] == {"t5.i1": "Отпуск 2027", "t5.i2": "Книги"}
    assert ctx.ref("t5.f1").field_id == PAGE_TITLE_FIELD_ID


def test_reverse_lookups():
    ctx = build()
    assert ctx.target_key("ds-buy") == "t2"
    assert ctx.ref("t2").target_id == "ds-buy" and ctx.ref("t2").kind == "target"
    assert ctx.ref("t2.f2.o1").option_id == "o-Rimi" and ctx.ref("t2.f2.o1").name == "Rimi"
    assert ctx.ref("t2.i2").item_id == "b-milk"
    assert ctx.field_keys("t2") == ["t2.f1", "t2.f2", "t2.f3", "t2.f4", "t2.f5", "t2.f6"]
    assert ctx.option_keys("t2.f2") == ["t2.f2.o1", "t2.f2.o2", "t2.f2.o3"]
    assert ctx.item_keys("t5") == ["t5.i1", "t5.i2"]
    assert ctx.ref("nope") is None
    assert ctx.target_keys() == ["t1", "t2", "t3", "t4", "t5"]


def test_items_cap_and_no_ids_or_urls_in_payload():
    ctx = build(items_per_target=2)
    assert list(ctx.payload["targets"][1]["items"]) == ["t2.i1", "t2.i2"]
    text = ctx.json()
    assert "ds-buy" not in text and "notion.so" not in text and "b-milk" not in text


def test_pending_and_now_default(monkeypatch):
    ctx = ContextBuilder("Europe/Tallinn").build(sample_snapshot(), pending={"question": "Куда?"})
    assert ctx.payload["pending"] == {"question": "Куда?"}
    assert ctx.now.tzinfo is not None
    ctx2 = ContextBuilder("UTC").build(sample_snapshot(), now=datetime(2026, 1, 5, 12, tzinfo=ZoneInfo("UTC")))
    assert ctx2.payload["weekday"] == "понедельник"


def test_json_is_compact_and_unicode():
    ctx = build()
    s = ctx.json()
    assert '"name":"Покупки"' in s and "\\u" not in s
    assert json.loads(s)["targets"][0]["name"] == "Дом"
```

- [ ] **Step 3:** run → ModuleNotFoundError.

- [ ] **Step 4: `app/llm/context.py`**

```python
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from app.notion.snapshot import Field, Target, WorkspaceSnapshot

RU_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
PAGE_TITLE_FIELD_ID = "__title__"
OPTION_TYPES = frozenset({"select", "status", "multi_select", "relation"})
_PAGE_TITLE = Field(id=PAGE_TITLE_FIELD_ID, name="Заголовок", type="title", required=True, options=[],
                    relation_data_source_id=None, description="Название новой подстраницы")


@dataclass(frozen=True)
class KeyRef:
    kind: Literal["target", "field", "option", "item"]
    target_id: str
    name: str
    field_id: str | None = None
    field_type: str | None = None
    option_id: str | None = None
    item_id: str | None = None


@dataclass
class Context:
    payload: dict
    keys: dict[str, KeyRef]
    now: datetime
    pending: dict | None = None
    fields_by_target: dict[str, list[str]] = field(default_factory=dict)
    options_by_field: dict[str, list[str]] = field(default_factory=dict)
    items_by_target: dict[str, list[str]] = field(default_factory=dict)

    def json(self) -> str:
        return json.dumps(self.payload, ensure_ascii=False, separators=(",", ":"))

    def ref(self, key: str) -> KeyRef | None:
        return self.keys.get(key)

    def target_keys(self) -> list[str]:
        return [k for k, r in self.keys.items() if r.kind == "target"]

    def target_key(self, target_id: str) -> str | None:
        for k, r in self.keys.items():
            if r.kind == "target" and r.target_id == target_id:
                return k
        return None

    def field_keys(self, target_key: str) -> list[str]:
        return list(self.fields_by_target.get(target_key, []))

    def option_keys(self, field_key: str) -> list[str]:
        return list(self.options_by_field.get(field_key, []))

    def item_keys(self, target_key: str) -> list[str]:
        return list(self.items_by_target.get(target_key, []))


class ContextBuilder:
    def __init__(self, timezone: str = "Europe/Tallinn", items_per_target: int = 50) -> None:
        self._tz = ZoneInfo(timezone)
        self._items_per_target = items_per_target

    def build(
        self, snapshot: WorkspaceSnapshot, now: datetime | None = None, pending: dict | None = None
    ) -> Context:
        now = (now or datetime.now(self._tz)).astimezone(self._tz)
        ctx = Context(payload={}, keys={}, now=now, pending=pending)
        targets = []
        for ti, t in enumerate(snapshot.targets, start=1):
            tk = f"t{ti}"
            ctx.keys[tk] = KeyRef("target", t.id, t.name)
            entry: dict = {
                "key": tk, "kind": t.kind, "name": t.name, "path": t.path,
                "ops": sorted(op.replace("create_page", "create") for op in t.operations),
            }
            if t.description:
                entry["description"] = t.description
            entry["fields"] = self._fields(ctx, tk, t)
            items: dict[str, str] = {}
            for ii, it in enumerate(t.items[: self._items_per_target], start=1):
                ik = f"{tk}.i{ii}"
                ctx.keys[ik] = KeyRef("item", t.id, it.title, item_id=it.id)
                items[ik] = f"{it.title} ({it.hint})" if it.hint else it.title
            ctx.items_by_target[tk] = list(items)
            entry["items" if t.kind == "database" else "children"] = items
            targets.append(entry)
        ctx.payload = {
            "now": now.isoformat(timespec="minutes"),
            "tz": str(self._tz),
            "weekday": RU_WEEKDAYS[now.weekday()],
            "targets": targets,
        }
        if pending:
            ctx.payload["pending"] = pending
        return ctx

    @staticmethod
    def _fields(ctx: Context, tk: str, t: Target) -> list[dict]:
        source = list(t.fields) if t.kind == "database" else [_PAGE_TITLE]
        out: list[dict] = []
        n = 0
        for f in source:
            if not f.writable or (f.type in OPTION_TYPES and not f.options):
                continue
            n += 1
            fk = f"{tk}.f{n}"
            ctx.keys[fk] = KeyRef("field", t.id, f.name, field_id=f.id, field_type=f.type)
            entry: dict = {"key": fk, "name": f.name, "type": f.type}
            if f.required:
                entry["required"] = True
            if f.description:
                entry["description"] = f.description
            if f.type in OPTION_TYPES:
                opts: dict[str, str] = {}
                for oi, o in enumerate(f.options, start=1):
                    ok = f"{fk}.o{oi}"
                    ctx.keys[ok] = KeyRef("option", t.id, o.name, field_id=f.id, field_type=f.type,
                                          option_id=o.id)
                    opts[ok] = o.name
                ctx.options_by_field[fk] = list(opts)
                entry["options"] = opts
            out.append(entry)
        ctx.fields_by_target[tk] = [e["key"] for e in out]
        return out
```

- [ ] **Step 5:** tests pass (7). Ruff. Commit `feat: LLM context builder with per-request keys`.

---

### Task 3: Output JSON schema

**Files:**
- Create: `app/llm/output_schema.py`, `tests/test_output_schema.py`
- Modify: `pyproject.toml` dev group: add `"jsonschema>=4.23"`; run `uv sync`.

**Interfaces:**
- Produces: `build_schema(ctx: Context) -> dict`, `INTENTS`, helpers `value_schema(ctx, field_key)`, `field_value_schema(ctx, field_key)`, `candidate_schema(ctx, target_key)`.

- [ ] **Step 1: failing tests** — `tests/test_output_schema.py`:

```python
import jsonschema
import pytest
from jsonschema import Draft7Validator

from app.interpretation.models import Interpretation
from app.llm.context import ContextBuilder
from app.llm.output_schema import build_schema, candidate_schema, value_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


@pytest.fixture
def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def test_schema_is_valid_draft7(ctx):
    Draft7Validator.check_schema(build_schema(ctx))


def test_value_schemas_by_type(ctx):
    assert value_schema(ctx, "t2.f1") == {"type": "string"}                       # title
    assert value_schema(ctx, "t2.f2") == {"enum": ["t2.f2.o1", "t2.f2.o2", "t2.f2.o3"]}  # select
    assert value_schema(ctx, "t2.f4") == {"type": "number"}
    assert value_schema(ctx, "t2.f5") == {"type": "boolean"}
    date = value_schema(ctx, "t3.f3")
    assert date["properties"]["start"] == {"type": "string"} and "end" in date["properties"]
    assert value_schema(ctx, "t3.f6")["items"]["enum"] == ["t3.f6.o1", "t3.f6.o2", "t3.f6.o3"]  # multi
    assert value_schema(ctx, "t3.f5")["items"]["enum"] == ["t3.f5.o1", "t3.f5.o2"]              # relation


def test_candidate_schema_pins_target_and_all_fields(ctx):
    c = candidate_schema(ctx, "t2")
    assert c["properties"]["target"] == {"const": "t2"}
    assert set(c["properties"]["fields"]["required"]) == set(ctx.field_keys("t2"))
    assert c["properties"]["fields"]["additionalProperties"] is False
    assert c["properties"]["item"]["anyOf"][0]["enum"] == ctx.item_keys("t2")
    home = candidate_schema(ctx, "t1")  # page with no children
    assert home["properties"]["item"] == {"type": "null"}
    assert home["properties"]["item_candidates"]["maxItems"] == 0


def test_valid_response_passes_schema_and_pydantic(ctx):
    schema = build_schema(ctx)
    raw = {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [{
            "target": "t2", "confidence": 0.9, "item": None, "item_candidates": [],
            "fields": {
                "t2.f1": {"status": "value", "value": "Молоко", "confidence": 0.99, "source_text": "молоко"},
                "t2.f2": {"status": "value", "value": "t2.f2.o1", "confidence": 0.9, "source_text": "в Рими"},
                "t2.f3": {"status": "not_mentioned"},
                "t2.f4": {"status": "ambiguous", "candidates": [1, 2], "source_text": "пару"},
                "t2.f5": {"status": "explicit_null"},
                "t2.f6": {"status": "not_mentioned"},
            },
            "content": None, "search_query": None,
        }],
        "notes": "",
    }
    jsonschema.validate(raw, schema)
    Interpretation.model_validate(raw)


@pytest.mark.parametrize("mutate", [
    lambda r: r["candidates"][0].__setitem__("target", "t9"),
    lambda r: r["candidates"][0]["fields"].__setitem__("t2.f2", {"status": "value", "value": "Rimi",
                                                                   "confidence": 1, "source_text": ""}),
    lambda r: r["candidates"][0]["fields"].pop("t2.f6"),
    lambda r: r["candidates"][0].__setitem__("item", "t3.i1"),
    lambda r: r["candidates"][0].__setitem__("url", "http://x"),
    lambda r: r.__setitem__("candidates", []),
])
def test_invalid_responses_fail_schema(ctx, mutate):
    schema = build_schema(ctx)
    raw = {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [{
            "target": "t2", "confidence": 0.9, "item": None, "item_candidates": [],
            "fields": {k: {"status": "not_mentioned"} for k in ctx.field_keys("t2")},
            "content": None, "search_query": None,
        }],
        "notes": "",
    }
    mutate(raw)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(raw, schema)


def test_schema_size_reasonable(ctx):
    import json

    assert len(json.dumps(build_schema(ctx))) < 60_000
```

- [ ] **Step 2:** run → ModuleNotFoundError (after adding jsonschema and `uv sync`).

- [ ] **Step 3: `app/llm/output_schema.py`**

```python
"""JSON schema for the LLM answer, specialised to one request's context keys."""

from __future__ import annotations

from app.llm.context import Context

INTENTS = ["create", "update", "append", "search", "unknown"]
STRING = {"type": "string"}
NUMBER = {"type": "number"}


def _obj(props: dict, required: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props) if required is None else required,
        "additionalProperties": False,
    }


def _nullable(schema: dict) -> dict:
    return {"anyOf": [schema, {"type": "null"}]}


def value_schema(ctx: Context, field_key: str) -> dict:
    ref = ctx.ref(field_key)
    if ref is None or ref.kind != "field":
        raise KeyError(field_key)
    t = ref.field_type
    if t in ("title", "rich_text", "url"):
        return dict(STRING)
    if t == "number":
        return dict(NUMBER)
    if t == "checkbox":
        return {"type": "boolean"}
    if t == "date":
        return _obj({"start": dict(STRING), "end": _nullable(dict(STRING))})
    if t in ("select", "status"):
        return {"enum": ctx.option_keys(field_key)}
    if t in ("multi_select", "relation"):
        return {"type": "array", "items": {"enum": ctx.option_keys(field_key)}}
    raise ValueError(f"unsupported field type {t!r}")


def field_value_schema(ctx: Context, field_key: str) -> dict:
    v = value_schema(ctx, field_key)
    return {"anyOf": [
        _obj({"status": {"const": "not_mentioned"}}),
        _obj({"status": {"const": "explicit_null"}}),
        _obj({"status": {"const": "ambiguous"}, "candidates": {"type": "array", "items": v},
              "source_text": dict(STRING)}),
        _obj({"status": {"const": "value"}, "value": v, "confidence": dict(NUMBER),
              "source_text": dict(STRING)}),
    ]}


def candidate_schema(ctx: Context, target_key: str) -> dict:
    fields = {fk: field_value_schema(ctx, fk) for fk in ctx.field_keys(target_key)}
    items = ctx.item_keys(target_key)
    item = _nullable({"enum": items}) if items else {"type": "null"}
    item_candidates = (
        {"type": "array", "items": {"enum": items}} if items else {"type": "array", "maxItems": 0}
    )
    return _obj({
        "target": {"const": target_key},
        "confidence": dict(NUMBER),
        "item": item,
        "item_candidates": item_candidates,
        "fields": _obj(fields),
        "content": _nullable(dict(STRING)),
        "search_query": _nullable(dict(STRING)),
    })


def build_schema(ctx: Context) -> dict:
    return _obj({
        "intent": _obj({"value": {"enum": INTENTS}, "confidence": dict(NUMBER)}),
        "candidates": {
            "type": "array", "minItems": 1, "maxItems": 3,
            "items": {"anyOf": [candidate_schema(ctx, tk) for tk in ctx.target_keys()]},
        },
        "notes": dict(STRING),
    })
```

- [ ] **Step 4:** tests pass. Ruff. Commit `feat: per-request JSON schema for LLM output` (include `pyproject.toml`, `uv.lock`).

---

### Task 4: LLM base types, prompt, Ollama client

**Files:**
- Create: `app/llm/base.py`, `app/llm/prompts.py`, `app/llm/ollama.py`, `tests/test_prompts.py`, `tests/test_ollama.py`

**Interfaces:**
- `base.py`: `class LLMError(Exception)`, `class LLMUnavailable(LLMError)`, `class LLMInvalidOutput(LLMError)` with attribute `raw: str`; `@dataclass LLMTrace(model: str, messages: list[dict], raw_response: str, duration_ms: int, attempts: int)`; `class LLMClient(Protocol)` with `async def interpret(self, text: str, context: Context, schema: dict) -> tuple[Interpretation, LLMTrace]` and `async def models(self) -> list[str]`.
- `prompts.py`: `SYSTEM_PROMPT: str`; `build_messages(text: str, ctx: Context) -> list[dict]` (system + one user message containing context JSON and the quoted user text); `retry_message(error: str) -> str`.
- `ollama.py`: `THINKING_FAMILIES = ("qwen3", "deepseek-r1", "gpt-oss", "magistral")`; `wants_think_flag(model) -> bool`; `class OllamaClient(base_url, model, *, temperature=0.0, num_ctx=16384, timeout_s=120.0, transport=None)` with `interpret`, `models`, `aclose`, async context manager.

- [ ] **Step 1: `app/llm/base.py`**

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.interpretation.models import Interpretation
from app.llm.context import Context


class LLMError(Exception):
    pass


class LLMUnavailable(LLMError):
    pass


class LLMInvalidOutput(LLMError):
    def __init__(self, message: str, raw: str = "") -> None:
        super().__init__(message)
        self.raw = raw


@dataclass
class LLMTrace:
    model: str
    messages: list[dict]
    raw_response: str
    duration_ms: int
    attempts: int


class LLMClient(Protocol):
    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]: ...

    async def models(self) -> list[str]: ...
```

- [ ] **Step 2: `app/llm/prompts.py`**

```python
"""Russian system prompt and message assembly. The JSON schema enforces the shape; the prompt
explains the semantics."""

from __future__ import annotations

from app.llm.context import Context

SYSTEM_PROMPT = """Ты — модуль интерпретации команд личного ассистента для Notion. Ты ничего не выполняешь: \
ты разбираешь сообщение пользователя и возвращаешь JSON строго по заданной схеме.

В сообщении пользователя есть контекст (JSON): текущее время now, часовой пояс tz, день недели weekday и \
список целей targets. Цель — база (kind=database) или страница (kind=page). У цели есть key, название, \
путь, описание, поля fields (key, название, тип, обязательность, допустимые варианты options) и \
существующие элементы items (для базы) или children (для страницы): ключ → название.

Порядок разбора:
1. intent: create — добавить запись в базу или подстраницу; update — изменить существующий элемент; \
append — дописать текст на страницу; search — найти; unknown — сообщение не про Notion.
2. target: только key из targets. Если правдоподобно несколько целей — верни до трёх кандидатов, \
лучший первым, у каждого своя confidence от 0 до 1. Выбор цели меняет смысл: «купи хлеб» для списка \
покупок — запись «Хлеб», для задач — задача «Купить хлеб».
3. Для каждого кандидата заполни ВСЕ его поля. status поля:
   value — значение названо или однозначно следует из текста; укажи confidence и source_text (фрагмент \
исходного сообщения);
   ambiguous — поле упомянуто, но подходят несколько вариантов; перечисли candidates;
   explicit_null — пользователь явно просит очистить/убрать значение;
   not_mentioned — в сообщении об этом ничего нет.
4. Для update и append выбери item из items/children выбранной цели. Подходит несколько — item=null и \
перечисли item_candidates. Не подходит ничего — item=null и пустой item_candidates.
5. Текст для append или тело новой подстраницы — в content. Поисковая фраза — в search_query.

Правила:
- Используй только ключи из контекста. Не придумывай поля, варианты и элементы.
- select/status/relation/multi_select: значение — только ключ из options. Если названный вариант не \
совпадает ни с одним — ambiguous (если похожи несколько) или not_mentioned. Никогда не угадывай.
- Даты считай от now с учётом weekday. start в формате YYYY-MM-DD или YYYY-MM-DDTHH:MM. При \
неоднозначной формулировке («в понедельник», «на следующей неделе») снижай confidence.
- Заголовок (title): коротко, по-русски, без командного глагола для списков предметов; для задач — \
инфинитив («Купить билеты»).
- Если поле необязательное и не упомянуто — not_mentioned, не заполняй по умолчанию.
- notes — короткая заметка о сомнениях, может быть пустой строкой.
Отвечай только JSON."""


def build_messages(text: str, ctx: Context) -> list[dict]:
    user = f"Контекст:\n{ctx.json()}\n\nСообщение пользователя:\n«{text.strip()}»"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


def retry_message(error: str) -> str:
    return (
        "Предыдущий ответ не прошёл проверку: "
        f"{error}\nВерни исправленный JSON строго по схеме, без пояснений."
    )
```

- [ ] **Step 3: failing tests** — `tests/test_prompts.py`:

```python
from app.llm.context import ContextBuilder
from app.llm.prompts import SYSTEM_PROMPT, build_messages, retry_message
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def test_messages_structure():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    msgs = build_messages("  купи молоко ", ctx)
    assert [m["role"] for m in msgs] == ["system", "user"]
    assert msgs[0]["content"] == SYSTEM_PROMPT
    assert msgs[1]["content"].endswith("«купи молоко»")
    assert '"key":"t2"' in msgs[1]["content"] and "2026-09-09T18:00+03:00" in msgs[1]["content"]


def test_system_prompt_mentions_rules():
    for needle in ("not_mentioned", "ambiguous", "explicit_null", "item_candidates", "search_query",
                   "только ключи", "YYYY-MM-DD"):
        assert needle in SYSTEM_PROMPT


def test_retry_message_contains_error():
    assert "поле X" in retry_message("поле X")
```

`tests/test_ollama.py`:

```python
import json

import httpx
import pytest

from app.llm.base import LLMInvalidOutput, LLMUnavailable
from app.llm.context import ContextBuilder
from app.llm.ollama import OllamaClient, wants_think_flag
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


@pytest.fixture
def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def good_answer(ctx):
    return {
        "intent": {"value": "create", "confidence": 0.95},
        "candidates": [{
            "target": "t2", "confidence": 0.9, "item": None, "item_candidates": [],
            "fields": {k: {"status": "not_mentioned"} for k in ctx.field_keys("t2")},
            "content": None, "search_query": None,
        }],
        "notes": "",
    }


def make(handler, model="qwen3:8b"):
    return OllamaClient("http://ollama.test", model, transport=httpx.MockTransport(handler))


def test_wants_think_flag():
    assert wants_think_flag("qwen3:8b") and wants_think_flag("deepseek-r1:7b")
    assert not wants_think_flag("qwen2.5:7b-instruct") and not wants_think_flag("gemma3:4b")


async def test_interpret_happy_path(ctx):
    seen = {}

    def handler(req: httpx.Request):
        seen["path"] = req.url.path
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"role": "assistant",
                                                     "content": json.dumps(good_answer(ctx))}})

    async with make(handler) as c:
        interp, trace = await c.interpret("купи молоко", ctx, build_schema(ctx))
    assert seen["path"] == "/api/chat"
    b = seen["body"]
    assert b["model"] == "qwen3:8b" and b["stream"] is False and b["think"] is False
    assert b["options"] == {"temperature": 0.0, "num_ctx": 16384}
    assert b["format"]["properties"]["candidates"]["maxItems"] == 3
    assert b["messages"][0]["role"] == "system" and "купи молоко" in b["messages"][1]["content"]
    assert interp.best.target == "t2"
    assert trace.attempts == 1 and trace.model == "qwen3:8b" and trace.duration_ms >= 0
    assert json.loads(trace.raw_response)["intent"]["value"] == "create"


async def test_no_think_flag_for_other_models(ctx):
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"message": {"content": json.dumps(good_answer(ctx))}})

    async with make(handler, model="gemma3:4b") as c:
        await c.interpret("x", ctx, build_schema(ctx))
    assert "think" not in seen["body"]


async def test_retry_once_on_invalid_then_success(ctx):
    calls = []

    def handler(req):
        body = json.loads(req.content)
        calls.append(body["messages"])
        if len(calls) == 1:
            return httpx.Response(200, json={"message": {"content": '{"intent": "bad"}'}})
        return httpx.Response(200, json={"message": {"content": json.dumps(good_answer(ctx))}})

    async with make(handler) as c:
        interp, trace = await c.interpret("x", ctx, build_schema(ctx))
    assert trace.attempts == 2 and len(calls) == 2
    assert calls[1][-2] == {"role": "assistant", "content": '{"intent": "bad"}'}
    assert calls[1][-1]["role"] == "user" and "не прошёл проверку" in calls[1][-1]["content"]


async def test_invalid_twice_raises(ctx):
    def handler(req):
        return httpx.Response(200, json={"message": {"content": "not json"}})

    async with make(handler) as c:
        with pytest.raises(LLMInvalidOutput) as ei:
            await c.interpret("x", ctx, build_schema(ctx))
    assert ei.value.raw == "not json"


async def test_unavailable(ctx):
    def boom(req):
        raise httpx.ConnectError("refused")

    async with make(boom) as c:
        with pytest.raises(LLMUnavailable):
            await c.interpret("x", ctx, build_schema(ctx))

    def err(req):
        return httpx.Response(500, text="boom")

    async with make(err) as c:
        with pytest.raises(LLMUnavailable):
            await c.interpret("x", ctx, build_schema(ctx))


async def test_models(ctx):
    def handler(req):
        assert req.url.path == "/api/tags"
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "gemma3:4b"}]})

    async with make(handler) as c:
        assert await c.models() == ["qwen3:8b", "gemma3:4b"]
```

- [ ] **Step 4:** run → ModuleNotFoundError.

- [ ] **Step 5: `app/llm/ollama.py`**

```python
from __future__ import annotations

import time

import httpx
from pydantic import ValidationError

from app.interpretation.models import Interpretation
from app.llm.base import LLMInvalidOutput, LLMTrace, LLMUnavailable
from app.llm.context import Context
from app.llm.prompts import build_messages, retry_message

THINKING_FAMILIES = ("qwen3", "deepseek-r1", "gpt-oss", "magistral")
MAX_ATTEMPTS = 2


def wants_think_flag(model: str) -> bool:
    family = model.split(":", 1)[0].lower()
    return family.startswith(THINKING_FAMILIES)


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.0,
        num_ctx: int = 16384,
        timeout_s: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self._temperature = temperature
        self._num_ctx = num_ctx
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=timeout_s,
                                         transport=transport)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> OllamaClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def models(self) -> list[str]:
        data = await self._call("GET", "/api/tags")
        return [m["name"] for m in data.get("models", [])]

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        messages = build_messages(text, context)
        start = time.monotonic()
        raw = ""
        error = ""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            body: dict = {
                "model": self.model,
                "messages": messages,
                "stream": False,
                "format": schema,
                "options": {"temperature": self._temperature, "num_ctx": self._num_ctx},
            }
            if wants_think_flag(self.model):
                body["think"] = False
            data = await self._call("POST", "/api/chat", body)
            raw = str(data.get("message", {}).get("content", ""))
            try:
                interp = Interpretation.model_validate_json(raw)
            except ValidationError as e:
                error = str(e)[:800]
                messages = [*messages, {"role": "assistant", "content": raw},
                            {"role": "user", "content": retry_message(error)}]
                continue
            return interp, LLMTrace(
                model=self.model, messages=messages, raw_response=raw,
                duration_ms=int((time.monotonic() - start) * 1000), attempts=attempt,
            )
        raise LLMInvalidOutput(f"invalid LLM output after {MAX_ATTEMPTS} attempts: {error}", raw=raw)

    async def _call(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            resp = await self._client.request(method, path, json=json)
        except httpx.HTTPError as e:
            raise LLMUnavailable(f"ollama unreachable: {type(e).__name__}") from None
        if resp.status_code >= 400:
            raise LLMUnavailable(f"ollama {resp.status_code}: {resp.text[:200]}")
        return resp.json()
```

- [ ] **Step 6:** tests pass. Ruff. Commit `feat: Ollama client with schema-constrained interpretation`.

---

### Task 5: Case fixtures and benchmark tool

**Files:**
- Create: `tests/fixtures/ru_cases.yaml`, `tools/benchmark_llm.py`, `tests/test_benchmark.py`, `tests/test_llm_integration.py`

**Interfaces:**
- `tools/benchmark_llm.py`: `load_cases(path) -> list[dict]`; `resolve_value(ctx, field_key, value) -> Any` (option keys → names, lists → list of names, date → dict); `score_case(case, interp, ctx) -> CaseResult`; `summarize(results) -> Summary`; `async run_model(client, cases, ctx, schema, limit=None) -> list[CaseResult]`; `render_table(rows) -> str`; `main(argv)`.
- Case format (YAML list):
  - `id`, `text` (required)
  - `intent`: expected intent
  - `target`: expected best target name, or `targets_any`: list of acceptable best-target names
  - `min_candidates`: int (ambiguity cases)
  - `item`: expected item title (for update/append) or `item_candidates_min`: int
  - `fields`: `{field name: expected}` — expected is a string (case-insensitive equality; `"*"` = any value), number, bool, or ISO date (matched against `start` prefix)
  - `statuses`: `{field name: status | [statuses]}` expected status
  - `max_confidence`: `{field name: float}` field confidence must be ≤ (for temporal ambiguity)
  - `content`: `"*"`; `search_query`: `"*"`
- `CaseResult(id, valid, intent_ok, target_ok, item_ok, fields_ok, all_ok, ms, error)`; `Summary(model, n, valid, intent, target, fields, all, p50_ms, p95_ms)` (rates as floats 0..1).

- [ ] **Step 1: `tests/fixtures/ru_cases.yaml`** (44 cases; `now` is Wednesday 2026-09-09 18:00):

```yaml
# Classification: create
- {id: buy_simple, text: "купи молоко в Рими", intent: create, target: Покупки,
   fields: {Название: Молоко, Магазин: Rimi}, statuses: {Категория: not_mentioned, Куплено: not_mentioned}}
- {id: buy_explicit_list, text: "добавь хлеб в покупки", intent: create, target: Покупки, fields: {Название: Хлеб}}
- {id: buy_with_category, text: "в список покупок: молоток, это инструменты", intent: create, target: Покупки,
   fields: {Название: Молоток, Категория: Инструменты}}
- {id: buy_quantity, text: "купить 6 яиц", intent: create, target: Покупки, fields: {Количество: 6}}
- {id: buy_shop_prisma, text: "надо взять сыр в присме", intent: create, target: Покупки, fields: {Название: Сыр, Magazin_placeholder: null}}
- {id: buy_note, text: "купить кофе, лучше зерновой", intent: create, target: Покупки, fields: {Название: Кофе}, statuses: {Заметка: value}}
- {id: todo_explicit, text: "создай задачу купить молоко", intent: create, target: Задачи, fields: {Задача: "*"}}
- {id: todo_remember, text: "не забудь позвонить в банк", intent: create, target: Задачи, fields: {Задача: "*"}}
- {id: todo_priority, text: "задача: подготовить отчёт, приоритет A", intent: create, target: Задачи,
   fields: {Приоритет: A}}
- {id: todo_priority_words, text: "срочно оплатить счета", intent: create, target: Задачи, fields: {Приоритет: A}}
- {id: todo_project, text: "задача по работе: обновить резюме", intent: create, target: Задачи, fields: {Проект: Работа}}
- {id: todo_tags, text: "задача сходить к врачу, теги здоровье", intent: create, target: Задачи, fields: {Теги: [здоровье]}}
- {id: todo_link, text: "задача прочитать статью https://example.com/a", intent: create, target: Задачи,
   fields: {Ссылка: "https://example.com/a"}}
- {id: idea_page, text: "создай страницу отпуск 2028 в идеях", intent: create, target: Идеи, fields: {Заголовок: "*"}}
# Ambiguity
- {id: amb_bread, text: "добавь хлеб", intent: create, targets_any: [Покупки, Задачи], min_candidates: 2}
- {id: amb_tickets, text: "билеты", intent: create, targets_any: [Покупки, Задачи], min_candidates: 2}
# Missing required / invalid values
- {id: missing_priority, text: "добавь задачу подготовить документы", intent: create, target: Задачи,
   statuses: {Приоритет: not_mentioned}}
- {id: invalid_priority, text: "задача убрать гараж, приоритет срочный", intent: create, target: Задачи,
   statuses: {Приоритет: [value, ambiguous, not_mentioned]}}
- {id: invalid_shop, text: "купить рыбу в Селвере", intent: create, target: Покупки,
   statuses: {Магазин: [not_mentioned, ambiguous]}}
- {id: explicit_null, text: "задача выбросить ёлку, без срока", intent: create, target: Задачи,
   statuses: {Срок: [explicit_null, not_mentioned]}}
# Optional fields preserved
- {id: optional_kept, text: "купить батарейки в Максиме, штук 10, хозтовары", intent: create, target: Покупки,
   fields: {Магазин: Maxima, Количество: 10, Категория: Хозтовары}}
# Temporal
- {id: date_tomorrow, text: "напомни завтра позвонить врачу", intent: create, target: Задачи, fields: {Срок: "2026-09-10"}}
- {id: date_day_after, text: "послезавтра сдать отчёт", intent: create, target: Задачи, fields: {Срок: "2026-09-11"}}
- {id: date_friday, text: "в пятницу забрать посылку", intent: create, target: Задачи, fields: {Срок: "2026-09-11"}}
- {id: date_next_monday, text: "в понедельник встреча с бухгалтером", intent: create, target: Задачи,
   fields: {Срок: "2026-09-14"}}
- {id: date_in_week, text: "через неделю продлить страховку", intent: create, target: Задачи, fields: {Срок: "2026-09-16"}}
- {id: date_time, text: "завтра в 9:45 к зубному", intent: create, target: Задачи, fields: {Срок: "2026-09-10T09:45"}}
- {id: date_next_week_vague, text: "на следующей неделе разобрать шкаф", intent: create, target: Задачи,
   statuses: {Срок: [value, ambiguous, not_mentioned]}, max_confidence: {Срок: 0.85}}
- {id: date_explicit, text: "15 октября заплатить за страховку", intent: create, target: Задачи, fields: {Срок: "2026-10-15"}}
# Update
- {id: update_bought, text: "отметь молоко купленным", intent: update, target: Покупки, item_candidates_min: 2}
- {id: update_bought_exact, text: "овсяное молоко куплено", intent: update, target: Покупки, item: Молоко овсяное,
   fields: {Куплено: true}}
- {id: update_status_done, text: "документы подготовил", intent: update, target: Задачи, item: Подготовить документы,
   fields: {Статус: Done}}
- {id: update_due, text: "перенеси покупку билетов на пятницу", intent: update, target: Задачи, item: Купить билеты,
   fields: {Срок: "2026-09-11"}}
- {id: update_priority, text: "подготовить документы теперь приоритет A", intent: update, target: Задачи,
   item: Подготовить документы, fields: {Приоритет: A}}
- {id: update_not_found, text: "отметь кефир купленным", intent: update, target: Покупки, item_candidates_min: 0,
   statuses: {Куплено: value}}
- {id: update_shop, text: "яйца лучше взять в Prisma", intent: update, target: Покупки, item: Яйца, fields: {Магазин: Prisma}}
# Append
- {id: append_ideas, text: "в идеи: попробовать сыр с плесенью", intent: append, target: Идеи, content: "*"}
- {id: append_trip, text: "допиши в отпуск 2027: взять палатку", intent: append, target: Идеи, item: Отпуск 2027, content: "*"}
- {id: append_books, text: "запиши в книги: Пелевин, iPhuck 10", intent: append, target: Идеи, item: Книги, content: "*"}
# Search
- {id: search_shop, text: "что у меня в покупках на Rimi?", intent: search, target: Покупки}
- {id: search_todo, text: "какие задачи по дому?", intent: search, target: Задачи}
- {id: search_bought, text: "что уже куплено?", intent: search, target: Покупки}
# Unknown
- {id: unknown_chat, text: "привет, как дела?", intent: unknown}
- {id: unknown_weather, text: "какая погода завтра", intent: unknown}
```
Then delete the placeholder in `buy_shop_prisma`: its `fields` must be `{Название: Сыр, Магазин: Prisma}` (fix while writing; the line above is a reminder that field names must match the sample workspace exactly).

- [ ] **Step 2: failing tests** — `tests/test_benchmark.py`:

```python
from pathlib import Path

from app.interpretation.models import Interpretation
from app.llm.context import ContextBuilder
from tools.benchmark_llm import CaseResult, load_cases, resolve_value, score_case, summarize
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

CASES = Path("tests/fixtures/ru_cases.yaml")


def ctx():
    return ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)


def interp(target="t2", intent="create", item=None, item_candidates=(), fields=None, content=None,
           search_query=None, extra_candidates=()):
    c = ctx()
    base = {k: {"status": "not_mentioned"} for k in c.field_keys(target)}
    base.update(fields or {})
    cands = [{"target": target, "confidence": 0.9, "item": item, "item_candidates": list(item_candidates),
              "fields": base, "content": content, "search_query": search_query}]
    for t in extra_candidates:
        cands.append({"target": t, "confidence": 0.5, "item": None, "item_candidates": [],
                      "fields": {k: {"status": "not_mentioned"} for k in c.field_keys(t)},
                      "content": None, "search_query": None})
    return Interpretation.model_validate({"intent": {"value": intent, "confidence": 0.9},
                                          "candidates": cands, "notes": ""})


def val(v, conf=0.9):
    return {"status": "value", "value": v, "confidence": conf, "source_text": ""}


def test_cases_load_and_reference_known_names():
    cases = load_cases(CASES)
    assert len(cases) >= 40
    c = ctx()
    names = {r.name for r in c.keys.values()}
    for case in cases:
        for key in ("target", *case.get("targets_any", [])):
            if key in case if key == "target" else True:
                name = case["target"] if key == "target" and "target" in case else key
                if name in case.get("targets_any", []) or key == "target" and "target" in case:
                    assert name in names, (case["id"], name)
        for fname in {**case.get("fields", {}), **case.get("statuses", {})}:
            assert fname in names, (case["id"], fname)
        if "item" in case:
            assert case["item"] in names, (case["id"], case["item"])


def test_resolve_value_maps_option_keys():
    c = ctx()
    assert resolve_value(c, "t2.f2", "t2.f2.o1") == "Rimi"
    assert resolve_value(c, "t3.f6", ["t3.f6.o1", "t3.f6.o3"]) == ["дом", "здоровье"]
    assert resolve_value(c, "t2.f1", "Молоко") == "Молоко"


def test_score_happy_case():
    case = {"id": "x", "text": "купи молоко в Рими", "intent": "create", "target": "Покупки",
            "fields": {"Название": "молоко", "Магазин": "Rimi"}, "statuses": {"Категория": "not_mentioned"}}
    r = score_case(case, interp(fields={"t2.f1": val("Молоко"), "t2.f2": val("t2.f2.o1")}), ctx())
    assert r.valid and r.intent_ok and r.target_ok and r.fields_ok and r.all_ok and r.item_ok


def test_score_field_mismatch_and_status():
    case = {"id": "x", "text": "", "intent": "create", "target": "Покупки", "fields": {"Магазин": "Prisma"}}
    r = score_case(case, interp(fields={"t2.f2": val("t2.f2.o1")}), ctx())
    assert r.target_ok and not r.fields_ok and not r.all_ok and "Магазин" in r.error
    case = {"id": "y", "text": "", "intent": "create", "target": "Покупки", "statuses": {"Магазин": ["ambiguous"]}}
    assert not score_case(case, interp(fields={"t2.f2": val("t2.f2.o1")}), ctx()).fields_ok


def test_score_date_prefix_wildcard_bool_number():
    case = {"id": "d", "text": "", "intent": "create", "target": "Задачи",
            "fields": {"Срок": "2026-09-10", "Задача": "*", "Приоритет": "A"}}
    i = interp(target="t3", fields={"t3.f3": val({"start": "2026-09-10T09:45", "end": None}),
                                    "t3.f1": val("Позвонить"), "t3.f2": val("t3.f2.o1")})
    assert score_case(case, i, ctx()).fields_ok
    case = {"id": "b", "text": "", "intent": "update", "target": "Покупки", "item": "Яйца",
            "fields": {"Куплено": True, "Количество": 6}}
    i = interp(intent="update", item="t2.i3", fields={"t2.f5": val(True), "t2.f4": val(6)})
    assert score_case(case, i, ctx()).all_ok


def test_score_ambiguity_items_content_search_max_conf():
    c = ctx()
    case = {"id": "a", "text": "", "intent": "create", "targets_any": ["Покупки", "Задачи"], "min_candidates": 2}
    assert score_case(case, interp(extra_candidates=["t3"]), c).all_ok
    assert not score_case(case, interp(), c).all_ok
    case = {"id": "i", "text": "", "intent": "update", "target": "Покупки", "item_candidates_min": 2}
    assert score_case(case, interp(intent="update", item_candidates=["t2.i2", "t2.i4"]), c).item_ok
    assert not score_case(case, interp(intent="update", item="t2.i2"), c).item_ok
    case = {"id": "c", "text": "", "intent": "append", "target": "Идеи", "content": "*"}
    assert score_case(case, interp(target="t5", intent="append", content="текст"), c).all_ok
    assert not score_case(case, interp(target="t5", intent="append", content=None), c).all_ok
    case = {"id": "m", "text": "", "intent": "create", "target": "Задачи", "max_confidence": {"Срок": 0.85}}
    assert score_case(case, interp(target="t3", fields={"t3.f3": val({"start": "2026-09-14", "end": None}, 0.6)}), c).fields_ok
    assert not score_case(case, interp(target="t3", fields={"t3.f3": val({"start": "2026-09-14", "end": None}, 0.95)}), c).fields_ok


def test_unknown_intent_case_needs_no_target():
    case = {"id": "u", "text": "", "intent": "unknown"}
    assert score_case(case, interp(intent="unknown"), ctx()).all_ok


def test_summarize():
    rs = [CaseResult("a", True, True, True, True, True, True, 100, ""),
          CaseResult("b", True, True, False, True, False, False, 300, "t"),
          CaseResult("c", False, False, False, False, False, False, 50, "invalid")]
    s = summarize("m", rs)
    assert s.n == 3 and s.valid == 2 / 3 and s.intent == 2 / 3 and s.target == 1 / 3 and s.all == 1 / 3
    assert s.p50_ms == 100 and s.p95_ms == 300
```

Simplify `test_cases_load_and_reference_known_names` to this exact body (the loop above is intentionally replaced):

```python
def test_cases_load_and_reference_known_names():
    cases = load_cases(CASES)
    assert len(cases) >= 40
    names = {r.name for r in ctx().keys.values()}
    for case in cases:
        for name in [case.get("target"), *case.get("targets_any", []), case.get("item")]:
            if name is not None:
                assert name in names, (case["id"], name)
        for fname in {**case.get("fields", {}), **case.get("statuses", {}), **case.get("max_confidence", {})}:
            assert fname in names, (case["id"], fname)
        assert case["intent"] in {"create", "update", "append", "search", "unknown"}
```

- [ ] **Step 3: `tools/benchmark_llm.py`**

```python
"""Benchmark Ollama models on the Russian case set.

uv run python -m tools.benchmark_llm --models qwen3:8b,qwen2.5:7b-instruct [--limit N] [--write documentation/BENCHMARK.md]
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.interpretation.models import Candidate, Interpretation, Value
from app.llm.base import LLMError
from app.llm.context import Context, ContextBuilder
from app.llm.ollama import OllamaClient
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

DEFAULT_CASES = Path("tests/fixtures/ru_cases.yaml")
INTENTS = {"create", "update", "append", "search", "unknown"}


@dataclass
class CaseResult:
    id: str
    valid: bool
    intent_ok: bool
    target_ok: bool
    item_ok: bool
    fields_ok: bool
    all_ok: bool
    ms: int
    error: str


@dataclass
class Summary:
    model: str
    n: int
    valid: float
    intent: float
    target: float
    fields: float
    all: float
    p50_ms: int
    p95_ms: int


def load_cases(path: Path) -> list[dict]:
    cases = yaml.safe_load(path.read_text(encoding="utf-8"))
    ids = [c["id"] for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate case ids")
    return cases


def _key_by_name(ctx: Context, kind: str, name: str, target_key: str | None = None) -> str | None:
    for k, r in ctx.keys.items():
        if r.kind != kind or r.name != name:
            continue
        if target_key and not k.startswith(target_key + "."):
            continue
        return k
    return None


def resolve_value(ctx: Context, field_key: str, value: Any) -> Any:
    if isinstance(value, list):
        return [resolve_value(ctx, field_key, v) for v in value]
    if isinstance(value, str):
        ref = ctx.ref(value)
        if ref is not None and ref.kind == "option":
            return ref.name
    return value


def _match(expected: Any, actual: Any) -> bool:
    if expected == "*":
        return actual not in (None, "", [], {})
    if isinstance(expected, bool):
        return actual is expected
    if isinstance(expected, int | float) and not isinstance(expected, bool):
        return isinstance(actual, int | float) and float(actual) == float(expected)
    if isinstance(expected, list):
        return isinstance(actual, list) and sorted(map(str, actual)) == sorted(map(str, expected))
    if isinstance(actual, dict) and "start" in actual:  # date
        return str(actual["start"]).startswith(str(expected))
    return str(actual).strip().casefold() == str(expected).strip().casefold()


def _best(interp: Interpretation) -> Candidate:
    return interp.best


def score_case(case: dict, interp: Interpretation, ctx: Context) -> CaseResult:
    errors: list[str] = []
    intent_ok = interp.intent.value == case["intent"]
    if not intent_ok:
        errors.append(f"intent {interp.intent.value}!={case['intent']}")
    best = _best(interp)
    best_name = ctx.ref(best.target).name if ctx.ref(best.target) else best.target

    if case["intent"] == "unknown" and "target" not in case and "targets_any" not in case:
        target_ok = True
    elif "targets_any" in case:
        target_ok = best_name in case["targets_any"]
    else:
        target_ok = best_name == case.get("target")
    if not target_ok:
        errors.append(f"target {best_name}")
    if "min_candidates" in case and len(interp.candidates) < case["min_candidates"]:
        target_ok = False
        errors.append(f"candidates {len(interp.candidates)}<{case['min_candidates']}")

    item_ok = True
    if "item" in case:
        item_name = ctx.ref(best.item).name if best.item and ctx.ref(best.item) else None
        item_ok = item_name == case["item"]
        if not item_ok:
            errors.append(f"item {item_name}")
    elif "item_candidates_min" in case:
        item_ok = best.item is None and len(best.item_candidates) >= case["item_candidates_min"]
        if not item_ok:
            errors.append(f"item_candidates {best.item_candidates} item={best.item}")

    fields_ok = True
    tk = best.target
    for fname, expected in case.get("fields", {}).items():
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if not isinstance(fv, Value) or not _match(expected, resolve_value(ctx, fk, fv.value)):
            fields_ok = False
            got = resolve_value(ctx, fk, fv.value) if isinstance(fv, Value) else getattr(fv, "status", None)
            errors.append(f"{fname}: {got!r} != {expected!r}")
    for fname, statuses in case.get("statuses", {}).items():
        allowed = statuses if isinstance(statuses, list) else [statuses]
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if fv is None or fv.status not in allowed:
            fields_ok = False
            errors.append(f"{fname}.status {getattr(fv, 'status', None)} not in {allowed}")
    for fname, limit in case.get("max_confidence", {}).items():
        fk = _key_by_name(ctx, "field", fname, tk)
        fv = best.fields.get(fk) if fk else None
        if isinstance(fv, Value) and fv.confidence > limit:
            fields_ok = False
            errors.append(f"{fname}.confidence {fv.confidence}>{limit}")
    if case.get("content") == "*" and not best.content:
        fields_ok = False
        errors.append("content empty")
    if case.get("search_query") == "*" and not best.search_query:
        fields_ok = False
        errors.append("search_query empty")

    all_ok = intent_ok and target_ok and item_ok and fields_ok
    return CaseResult(case["id"], True, intent_ok, target_ok, item_ok, fields_ok, all_ok, 0, "; ".join(errors))


def summarize(model: str, results: list[CaseResult]) -> Summary:
    n = max(len(results), 1)
    times = sorted(r.ms for r in results) or [0]
    p95 = times[min(len(times) - 1, int(round(0.95 * (len(times) - 1))))]
    return Summary(
        model=model, n=len(results),
        valid=sum(r.valid for r in results) / n,
        intent=sum(r.intent_ok for r in results) / n,
        target=sum(r.target_ok for r in results) / n,
        fields=sum(r.fields_ok for r in results) / n,
        all=sum(r.all_ok for r in results) / n,
        p50_ms=int(statistics.median(times)), p95_ms=int(p95),
    )


async def run_model(client: OllamaClient, cases: list[dict], ctx: Context, schema: dict,
                    limit: int | None = None) -> list[CaseResult]:
    out: list[CaseResult] = []
    for case in cases[:limit]:
        try:
            interp, trace = await client.interpret(case["text"], ctx, schema)
        except LLMError as e:
            out.append(CaseResult(case["id"], False, False, False, False, False, False, 0, str(e)[:200]))
            print(f"  {case['id']:<24} INVALID {str(e)[:80]}", flush=True)
            continue
        r = score_case(case, interp, ctx)
        r.ms = trace.duration_ms
        out.append(r)
        mark = "ok " if r.all_ok else "FAIL"
        print(f"  {case['id']:<24} {mark} {r.ms:>6} ms  {r.error}", flush=True)
    return out


def render_table(summaries: list[Summary]) -> str:
    head = "| model | n | valid | intent | target | fields | all | p50 ms | p95 ms |\n|---|---|---|---|---|---|---|---|---|"
    rows = [f"| {s.model} | {s.n} | {s.valid:.0%} | {s.intent:.0%} | {s.target:.0%} | {s.fields:.0%} | "
            f"{s.all:.0%} | {s.p50_ms} | {s.p95_ms} |" for s in summaries]
    return "\n".join([head, *rows])


async def main_async(a: argparse.Namespace) -> int:
    cases = load_cases(a.cases)
    ctx = ContextBuilder("Europe/Tallinn").build(sample_snapshot(), now=SAMPLE_NOW)
    schema = build_schema(ctx)
    summaries: list[Summary] = []
    failures: dict[str, list[CaseResult]] = {}
    for model in a.models.split(","):
        model = model.strip()
        print(f"\n== {model} ==", flush=True)
        async with OllamaClient(a.ollama, model, num_ctx=a.num_ctx, timeout_s=a.timeout) as client:
            results = await run_model(client, cases, ctx, schema, a.limit)
        summaries.append(summarize(model, results))
        failures[model] = [r for r in results if not r.all_ok]
    table = render_table(summaries)
    print("\n" + table)
    if a.write:
        lines = [f"# LLM benchmark — {datetime.now(UTC).date().isoformat()}", "",
                 f"Cases: `{a.cases}` ({len(cases)}), context: `tools/sample_workspace.py`, "
                 f"num_ctx={a.num_ctx}, temperature=0.", "", table, ""]
        for model, fails in failures.items():
            lines.append(f"## {model} failures ({len(fails)})")
            lines.extend(f"- `{r.id}`: {r.error}" for r in fails)
            lines.append("")
        a.write.write_text("\n".join(lines), encoding="utf-8")
        print(f"wrote {a.write}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", required=True, help="comma-separated Ollama model names")
    ap.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--ollama", default="http://127.0.0.1:11434")
    ap.add_argument("--num-ctx", type=int, default=16384)
    ap.add_argument("--timeout", type=float, default=180.0)
    ap.add_argument("--write", type=Path)
    return asyncio.run(main_async(ap.parse_args(argv)))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
```

- [ ] **Step 4: `tests/test_llm_integration.py`** (skipped by default):

```python
import httpx
import pytest

from app.llm.context import ContextBuilder
from app.llm.ollama import OllamaClient
from app.llm.output_schema import build_schema
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot

pytestmark = pytest.mark.integration
OLLAMA = "http://127.0.0.1:11434"


def ollama_up() -> bool:
    try:
        return httpx.get(f"{OLLAMA}/api/tags", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@pytest.mark.skipif(not ollama_up(), reason="ollama not running")
async def test_real_model_buy_milk():
    ctx = ContextBuilder().build(sample_snapshot(), now=SAMPLE_NOW)
    async with OllamaClient(OLLAMA, "qwen3:8b") as c:
        models = await c.models()
        assert "qwen3:8b" in models
        interp, trace = await c.interpret("купи молоко в Рими", ctx, build_schema(ctx))
    assert interp.intent.value == "create"
    assert ctx.ref(interp.best.target).name == "Покупки"
```

- [ ] **Step 5:** `uv run pytest -q` (integration excluded) passes; `uv run pytest -m integration -q` runs the real test if Ollama is up (record result). Ruff. Commit `feat: Russian case set and LLM benchmark tool`.

---

### Task 6: Run the benchmark, choose the model, record

**Files:**
- Create: `documentation/BENCHMARK.md`
- Modify (only if the winner is not `qwen3:8b`): `app/config.py` default `llm_model`, `.env.example`, `documentation/DATA_MODEL.md` §7, `documentation/ARCHITECTURE.md` §13.

- [ ] **Step 1:** Smoke one model on 5 cases: `uv run python -m tools.benchmark_llm --models qwen3:8b --limit 5`. If validity is 0% (grammar too strict / model returns garbage), inspect `trace.raw_response` via a quick script and adjust the prompt wording in `app/llm/prompts.py` (not the schema) before the full run. If a model rejects `think:false` with an HTTP 400 mentioning "think", remove that family from `THINKING_FAMILIES` and report.
- [ ] **Step 2:** Full run: `uv run python -m tools.benchmark_llm --models qwen3:8b,qwen2.5:7b-instruct,llama3.1:8b,gemma3:4b --write documentation/BENCHMARK.md`. Expect 10–40 min total.
- [ ] **Step 3:** Choose: highest `all` rate; ties broken by p50 latency. Add a "## Decision" section to `BENCHMARK.md` naming the model and the runner-up, plus the three most common failure patterns. If the winner differs from `qwen3:8b`, update the defaults listed above.
- [ ] **Step 4:** Commit `docs: LLM benchmark results and model choice` (plus config changes if any).

---

## Self-review

- Spec coverage: T-002 (Task 1), T-020 (Task 2), T-021 (Task 3), T-022 (Task 4 client), T-023 (Task 4 prompt), T-024 (Task 5), T-025 (Task 6). ARCHITECTURE §6 context shape implemented with `ops`/`children` naming; §14 boundaries: payload carries no ids/urls (tested).
- Type consistency: `Context.field_keys/option_keys/item_keys/ref/target_keys` used identically in `output_schema.py`, `benchmark_llm.py`, tests; `OllamaClient.interpret(text, context, schema)` matches `LLMClient`; `LLMTrace.duration_ms` used by `run_model`; `CaseResult` positional order `(id, valid, intent_ok, target_ok, item_ok, fields_ok, all_ok, ms, error)` matches `test_summarize`.
- Known simplifications: no few-shot examples (grammar constrains shape; benchmark decides if needed); pending-session content is passed through as an opaque dict for Plan 3; confidence bounds are enforced by Pydantic, not by the grammar.
