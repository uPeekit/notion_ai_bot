# Plan 1: Foundation + Notion Layer — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Project skeleton, settings, core data models, SQLite audit store, Notion REST provider, and workspace discovery that produces a `WorkspaceSnapshot` from a real Notion workspace.

**Architecture:** Single Python package `app/` with one responsibility per module. Notion access goes only through `NotionProvider` (protocol) implemented by `DirectNotionProvider` (httpx). `Discovery` turns provider results into an immutable `WorkspaceSnapshot`, enriched with human descriptions from `data/targets.yaml`. Everything is unit-tested with `httpx.MockTransport` and in-memory fakes; a CLI tool exercises the real API.

**Tech Stack:** Python 3.12 (uv-managed), uv, pydantic 2 + pydantic-settings, httpx, PyYAML, sqlite3 (stdlib), pytest + pytest-asyncio, ruff.

**Spec:** `documentation/ARCHITECTURE.md`, `documentation/DATA_MODEL.md`, `documentation/ERRORS.md`, `documentation/IMPLEMENTATION_PLAN.md` (tasks T-001…T-015).

## Global Constraints

- Python `>=3.12`; uv pins 3.12 via `.python-version`.
- Notion header `Notion-Version: 2025-09-03`; data-source endpoints only (`/v1/data_sources/...`), never `/v1/databases/{id}/query`.
- Secrets (`NOTION_TOKEN`, `TELEGRAM_BOT_TOKEN`) never logged, never stored in SQLite, never in exceptions' messages.
- Supported writable property types: `title, rich_text, select, multi_select, status, date, checkbox, number, url, relation`. Everything else → `readonly`.
- Runtime files under `data/` (gitignored). Tests use `tmp_path`.
- Commit after every task; message prefix `feat:`/`test:`/`chore:`; trailer `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- Run `uv run ruff check .` and `uv run pytest -q` before each commit; both must pass.
- Shell on this machine is PowerShell; commands below are PowerShell unless noted.

---

## File structure (this plan)

```text
pyproject.toml, .python-version, .env.example, README.md
app/__init__.py
app/config.py                 Settings
app/notion/__init__.py
app/notion/snapshot.py        WorkspaceSnapshot, Target, Field, Option, Item, WRITABLE_TYPES
app/notion/errors.py          NotionError
app/notion/provider.py        NotionProvider protocol
app/notion/direct.py          DirectNotionProvider (httpx)
app/notion/props.py           helpers: plain_text, page_title, property_kind
app/notion/descriptions.py    targets.yaml load/merge/save (TargetMeta, FieldMeta)
app/notion/discovery.py       Discovery: provider → snapshot, TTL cache
app/interpretation/__init__.py
app/interpretation/models.py  Intent, FieldValue union, Candidate, Interpretation
app/audit/__init__.py
app/audit/store.py            AuditStore (sqlite3)
tools/discover.py             prints snapshot tree from real Notion
tests/conftest.py             shared fixtures
tests/test_config.py
tests/test_snapshot_models.py
tests/test_interpretation_models.py
tests/test_audit_store.py
tests/test_notion_direct.py
tests/test_descriptions.py
tests/test_discovery.py
tests/fixtures/notion/*.json  canned Notion responses
```

---

### Task 1: Project skeleton with uv

**Files:**
- Create: `pyproject.toml`, `.python-version`, `app/__init__.py`, `tests/__init__.py`, `tests/conftest.py`, `.env.example`
- Modify: `README.md`, `.gitignore`

**Interfaces:**
- Produces: importable package `app`; `uv run pytest` and `uv run ruff check .` work.

- [ ] **Step 1: Install uv (skip if `uv --version` works)**

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
uv --version
```

- [ ] **Step 2: Write `pyproject.toml`**

```toml
[project]
name = "notion-ai-bot"
version = "0.1.0"
description = "Local Telegram → Notion assistant"
requires-python = ">=3.12"
dependencies = [
    "python-telegram-bot~=22.8",
    "httpx>=0.27",
    "pydantic>=2.9",
    "pydantic-settings>=2.5",
    "faster-whisper>=1.2",
    "pyyaml>=6.0",
]

[dependency-groups]
dev = [
    "pytest>=8.3",
    "pytest-asyncio>=0.24",
    "ruff>=0.6",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["app"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
markers = ["integration: needs real Ollama/Notion"]
addopts = "-m 'not integration'"

[tool.ruff]
line-length = 100
target-version = "py312"

[tool.ruff.lint]
select = ["E", "F", "I", "UP", "B"]
```

- [ ] **Step 3: Write `.python-version`, package inits, conftest**

`.python-version`:
```text
3.12
```

`app/__init__.py`: empty. `tests/__init__.py`: empty.

`tests/conftest.py`:
```python
import pytest


@pytest.fixture
def env(monkeypatch):
    """Minimal valid environment for Settings."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
    monkeypatch.setenv("NOTION_TOKEN", "ntn-test-token")
    return monkeypatch
```

- [ ] **Step 4: Write `.env.example`**

```dotenv
# Telegram
TELEGRAM_BOT_TOKEN=
TELEGRAM_ALLOWED_USER_IDS=123456789

# Notion (internal integration secret)
NOTION_TOKEN=
NOTION_VERSION=2025-09-03

# LLM
OLLAMA_BASE_URL=http://127.0.0.1:11434
LLM_MODEL=qwen3:8b
LLM_TEMPERATURE=0
LLM_NUM_CTX=16384
LLM_TIMEOUT_S=120

# Speech
WHISPER_MODEL=large-v3-turbo
WHISPER_DEVICE=auto
WHISPER_COMPUTE_TYPE=int8
WHISPER_LANGUAGE=ru

# Locale
TIMEZONE=Europe/Tallinn
LOCALE=ru

# Storage
DB_PATH=data/bot.sqlite
TARGETS_FILE=data/targets.yaml
SCHEMA_CACHE_TTL_S=60
ITEMS_PER_TARGET=50
ADMIN_UI_PORT=8787

# Policy thresholds (0..1)
POLICY_INTENT_MIN=0.85
POLICY_TARGET_MIN=0.85
POLICY_TARGET_MARGIN=0.10
POLICY_FIELD_MIN=0.75
POLICY_DATE_MIN=0.80

SESSION_TTL_S=900
UNDO_WINDOW_S=300
LOG_LEVEL=INFO
```

- [ ] **Step 5: Update `.gitignore` and README**

Append to `.gitignore`:
```text
*.egg-info/
dist/
```

Replace `README.md` with:
```markdown
# notion_ai_bot

Local Telegram → Notion assistant. Voice/text in Telegram, local Whisper + Ollama, deterministic validation, Notion REST.

Design: see `documentation/`.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) and run `uv sync`.
2. Copy `.env.example` to `.env` and fill tokens (see below).
3. `uv run python -m tools.discover` prints what the Notion integration can see.

### Notion integration
1. https://www.notion.so/profile/integrations → New integration (internal), copy the secret into `NOTION_TOKEN`.
2. In Notion, open each top-level page you want the bot to use → `...` → Connections → add the integration. Child pages and databases inherit access.

### Telegram bot
1. Talk to @BotFather → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id (e.g. from @userinfobot) → `TELEGRAM_ALLOWED_USER_IDS`.

## Development
- `uv run pytest -q`
- `uv run ruff check .`
```

- [ ] **Step 6: Sync and verify**

```powershell
uv sync
uv run pytest -q
uv run ruff check .
```
Expected: `uv sync` creates `.venv` with Python 3.12; pytest reports `no tests ran`; ruff `All checks passed!`.

- [ ] **Step 7: Commit**

```powershell
git add -A
git commit -m "chore: project skeleton with uv, pytest, ruff

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 2: Settings

**Files:**
- Create: `app/config.py`, `tests/test_config.py`

**Interfaces:**
- Produces: `class Settings(BaseSettings)` with fields exactly as in DATA_MODEL §7 (lowercase names); property `allowed_user_ids: frozenset[int]`; function `load_settings() -> Settings`.

- [ ] **Step 1: Write failing tests**

`tests/test_config.py`:
```python
import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_defaults_load(env):
    s = load_settings()
    assert s.notion_version == "2025-09-03"
    assert s.llm_model == "qwen3:8b"
    assert s.policy_target_margin == 0.10
    assert s.allowed_user_ids == frozenset({1, 2})
    assert s.timezone == "Europe/Tallinn"


def test_missing_token_fails(env):
    env.delenv("NOTION_TOKEN")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_bad_threshold_fails(env):
    env.setenv("POLICY_TARGET_MIN", "1.5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_allowlist_parsing(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " 10, 20 ,30 ")
    assert Settings(_env_file=None).allowed_user_ids == frozenset({10, 20, 30})


def test_empty_allowlist_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_config.py -q`
Expected: ImportError `app.config`.

- [ ] **Step 3: Implement `app/config.py`**

```python
from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Prob = Field(ge=0.0, le=1.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_allowed_user_ids: str
    notion_token: str
    notion_version: str = "2025-09-03"

    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:8b"
    llm_temperature: float = 0.0
    llm_num_ctx: int = 16384
    llm_timeout_s: float = 120.0

    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"

    timezone: str = "Europe/Tallinn"
    locale: str = "ru"

    db_path: Path = Path("data/bot.sqlite")
    targets_file: Path = Path("data/targets.yaml")
    schema_cache_ttl_s: int = 60
    items_per_target: int = 50
    admin_ui_port: int = 8787

    policy_intent_min: float = Field(0.85, ge=0.0, le=1.0)
    policy_target_min: float = Field(0.85, ge=0.0, le=1.0)
    policy_target_margin: float = Field(0.10, ge=0.0, le=1.0)
    policy_field_min: float = Field(0.75, ge=0.0, le=1.0)
    policy_date_min: float = Field(0.80, ge=0.0, le=1.0)

    session_ttl_s: int = 900
    undo_window_s: int = 300
    log_level: str = "INFO"

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _non_empty_allowlist(cls, v: str) -> str:
        ids = [p.strip() for p in v.split(",") if p.strip()]
        if not ids:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must list at least one user id")
        for p in ids:
            if not p.isdigit():
                raise ValueError(f"bad user id: {p!r}")
        return v

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.telegram_allowed_user_ids.split(",") if p.strip())


def load_settings() -> Settings:
    return Settings()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_config.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/config.py tests/test_config.py
git commit -m "feat: settings from .env with validation

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 3: Snapshot data model

**Files:**
- Create: `app/notion/__init__.py`, `app/notion/snapshot.py`, `tests/test_snapshot_models.py`

**Interfaces:**
- Produces (frozen dataclasses): `Option(id, name)`, `Item(id, title, hint, last_edited)`, `Field(id, name, type, required, options, relation_data_source_id, description)`, `Target(id, kind, name, path, description, parent_page_id, database_id, fields, items, operations, url)`, `WorkspaceSnapshot(fetched_at, targets)` with methods `target(id) -> Target | None`, `databases() -> list[Target]`, `pages() -> list[Target]`. Constants `WRITABLE_TYPES: frozenset[str]`, `FieldType = Literal[...]`, `DB_OPERATIONS = frozenset({"create","update","search"})`, `PAGE_OPERATIONS = frozenset({"create_page","append","search"})`.

- [ ] **Step 1: Write failing tests**

`tests/test_snapshot_models.py`:
```python
from datetime import UTC, datetime

from app.notion.snapshot import (
    DB_OPERATIONS,
    PAGE_OPERATIONS,
    WRITABLE_TYPES,
    Field,
    Item,
    Option,
    Target,
    WorkspaceSnapshot,
)


def make_db(id="ds1", name="Покупки"):
    return Target(
        id=id, kind="database", name=name, path=name, description="", parent_page_id=None,
        database_id="db1",
        fields=[
            Field(id="title", name="Название", type="title", required=True, options=[],
                  relation_data_source_id=None, description=""),
            Field(id="shop", name="Магазин", type="select", required=False,
                  options=[Option("o1", "Rimi")], relation_data_source_id=None, description=""),
        ],
        items=[Item(id="p1", title="Хлеб", hint=None, last_edited=datetime.now(UTC))],
        operations=DB_OPERATIONS, url="https://notion.so/ds1",
    )


def make_page(id="pg1", name="Идеи"):
    return Target(
        id=id, kind="page", name=name, path=name, description="", parent_page_id=None,
        database_id=None, fields=[], items=[], operations=PAGE_OPERATIONS, url="https://notion.so/pg1",
    )


def test_snapshot_lookup_and_partition():
    snap = WorkspaceSnapshot(fetched_at=datetime.now(UTC), targets=[make_db(), make_page()])
    assert snap.target("ds1").name == "Покупки"
    assert snap.target("nope") is None
    assert [t.id for t in snap.databases()] == ["ds1"]
    assert [t.id for t in snap.pages()] == ["pg1"]


def test_target_field_lookup():
    t = make_db()
    assert t.field("shop").name == "Магазин"
    assert t.field("x") is None
    assert t.title_field().id == "title"


def test_writable_types_fixed():
    assert WRITABLE_TYPES == frozenset(
        {"title", "rich_text", "select", "multi_select", "status", "date", "checkbox",
         "number", "url", "relation"}
    )
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_snapshot_models.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `app/notion/snapshot.py`** (and empty `app/notion/__init__.py`)

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

FieldType = Literal[
    "title", "rich_text", "select", "multi_select", "status", "date",
    "checkbox", "number", "url", "relation", "readonly",
]

WRITABLE_TYPES: frozenset[str] = frozenset(
    {"title", "rich_text", "select", "multi_select", "status", "date",
     "checkbox", "number", "url", "relation"}
)

DB_OPERATIONS: frozenset[str] = frozenset({"create", "update", "search"})
PAGE_OPERATIONS: frozenset[str] = frozenset({"create_page", "append", "search"})


@dataclass(frozen=True)
class Option:
    id: str
    name: str


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    hint: str | None
    last_edited: datetime


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    type: FieldType
    required: bool
    options: list[Option]
    relation_data_source_id: str | None
    description: str

    @property
    def writable(self) -> bool:
        return self.type in WRITABLE_TYPES


@dataclass(frozen=True)
class Target:
    id: str
    kind: Literal["database", "page"]
    name: str
    path: str
    description: str
    parent_page_id: str | None
    database_id: str | None
    fields: list[Field]
    items: list[Item]
    operations: frozenset[str]
    url: str

    def field(self, field_id: str) -> Field | None:
        return next((f for f in self.fields if f.id == field_id), None)

    def title_field(self) -> Field | None:
        return next((f for f in self.fields if f.type == "title"), None)

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    fetched_at: datetime
    targets: list[Target] = field(default_factory=list)

    def target(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def databases(self) -> list[Target]:
        return [t for t in self.targets if t.kind == "database"]

    def pages(self) -> list[Target]:
        return [t for t in self.targets if t.kind == "page"]
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_snapshot_models.py -q`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/notion tests/test_snapshot_models.py
git commit -m "feat: workspace snapshot data model

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 4: Interpretation models (Pydantic)

**Files:**
- Create: `app/interpretation/__init__.py`, `app/interpretation/models.py`, `tests/test_interpretation_models.py`

**Interfaces:**
- Produces: `Intent`, `NotMentioned`, `ExplicitNull`, `Ambiguous`, `Value`, `FieldValue` (discriminated union on `status`), `DateValue`, `Candidate`, `Interpretation`, `IntentName = Literal["create","update","append","search","unknown"]`. All `extra="forbid"`.

- [ ] **Step 1: Write failing tests**

`tests/test_interpretation_models.py`:
```python
import pytest
from pydantic import ValidationError

from app.interpretation.models import Candidate, Interpretation, Value

RAW = {
    "intent": {"value": "create", "confidence": 0.97},
    "candidates": [
        {
            "target": "t1",
            "confidence": 0.95,
            "item": None,
            "item_candidates": [],
            "fields": {
                "t1.f1": {"status": "value", "value": "Молоко", "confidence": 0.99,
                          "source_text": "молоко"},
                "t1.f2": {"status": "not_mentioned"},
                "t1.f3": {"status": "ambiguous", "candidates": ["t1.f3.o1", "t1.f3.o2"],
                          "source_text": "рими"},
                "t1.f4": {"status": "explicit_null"},
            },
            "content": None,
            "search_query": None,
        }
    ],
    "notes": "",
}


def test_roundtrip():
    interp = Interpretation.model_validate(RAW)
    assert interp.intent.value == "create"
    c = interp.candidates[0]
    assert isinstance(c.fields["t1.f1"], Value)
    assert c.fields["t1.f2"].status == "not_mentioned"
    assert c.fields["t1.f3"].candidates == ["t1.f3.o1", "t1.f3.o2"]
    assert Interpretation.model_validate_json(interp.model_dump_json()) == interp


def test_unknown_status_rejected():
    bad = {**RAW}
    bad["candidates"] = [{**RAW["candidates"][0], "fields": {"t1.f1": {"status": "maybe"}}}]
    with pytest.raises(ValidationError):
        Interpretation.model_validate(bad)


def test_confidence_bounds():
    with pytest.raises(ValidationError):
        Interpretation.model_validate({**RAW, "intent": {"value": "create", "confidence": 1.2}})


def test_candidates_min_one():
    with pytest.raises(ValidationError):
        Interpretation.model_validate({**RAW, "candidates": []})


def test_extra_keys_forbidden():
    with pytest.raises(ValidationError):
        Candidate.model_validate({**RAW["candidates"][0], "url": "http://evil"})


def test_best_and_second():
    two = {**RAW, "candidates": [RAW["candidates"][0],
                                 {**RAW["candidates"][0], "target": "t2", "confidence": 0.5}]}
    interp = Interpretation.model_validate(two)
    assert interp.best.target == "t1"
    assert interp.second.target == "t2"
    assert Interpretation.model_validate(RAW).second is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_interpretation_models.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `app/interpretation/models.py`** (and empty `__init__.py`)

```python
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

IntentName = Literal["create", "update", "append", "search", "unknown"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Intent(Strict):
    value: IntentName
    confidence: float = Field(ge=0.0, le=1.0)


class NotMentioned(Strict):
    status: Literal["not_mentioned"]


class ExplicitNull(Strict):
    status: Literal["explicit_null"]


class Ambiguous(Strict):
    status: Literal["ambiguous"]
    candidates: list[Any]
    source_text: str = ""


class Value(Strict):
    status: Literal["value"]
    value: Any
    confidence: float = Field(ge=0.0, le=1.0)
    source_text: str = ""


FieldValue = Annotated[NotMentioned | ExplicitNull | Ambiguous | Value, Field(discriminator="status")]


class DateValue(Strict):
    start: str
    end: str | None = None


class Candidate(Strict):
    target: str
    confidence: float = Field(ge=0.0, le=1.0)
    item: str | None = None
    item_candidates: list[str] = Field(default_factory=list)
    fields: dict[str, FieldValue] = Field(default_factory=dict)
    content: str | None = None
    search_query: str | None = None


class Interpretation(Strict):
    intent: Intent
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    notes: str = ""

    @property
    def best(self) -> Candidate:
        return max(self.candidates, key=lambda c: c.confidence)

    @property
    def second(self) -> Candidate | None:
        rest = sorted(self.candidates, key=lambda c: c.confidence, reverse=True)[1:]
        return rest[0] if rest else None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_interpretation_models.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/interpretation tests/test_interpretation_models.py
git commit -m "feat: interpretation pydantic models

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 5: Audit store (SQLite)

**Files:**
- Create: `app/audit/__init__.py`, `app/audit/store.py`, `tests/test_audit_store.py`

**Interfaces:**
- Produces: `class AuditStore(path: Path)` with `migrate()`, `new_event(**cols) -> int`, `update_event(event_id, **cols)`, `get_event(event_id) -> dict | None`, `save_session(chat_id, payload: str, expires_at: datetime)`, `get_session(chat_id, now) -> str | None` (None when expired; expired rows deleted), `delete_session(chat_id)`, `add_execution(event_id, chat_id, reply_message_id, undo: str, expires_at) -> int`, `get_execution(execution_id, now) -> dict | None`, `mark_undone(execution_id)`, `latest_execution(chat_id, now) -> dict | None`, `close()`. Datetimes are timezone-aware UTC; stored ISO.

- [ ] **Step 1: Write failing tests**

`tests/test_audit_store.py`:
```python
from datetime import UTC, datetime, timedelta

import pytest

from app.audit.store import AuditStore


@pytest.fixture
def store(tmp_path):
    s = AuditStore(tmp_path / "t.sqlite")
    s.migrate()
    yield s
    s.close()


def test_event_insert_update_read(store):
    eid = store.new_event(telegram_user_id=1, chat_id=1, kind="text", raw_input="купи хлеб")
    store.update_event(eid, decision="EXECUTE", executed=1, notion_page_id="p1", duration_ms=42)
    row = store.get_event(eid)
    assert row["raw_input"] == "купи хлеб"
    assert row["decision"] == "EXECUTE"
    assert row["executed"] == 1
    assert row["ts"].endswith("+00:00")


def test_unknown_column_rejected(store):
    with pytest.raises(ValueError):
        store.new_event(telegram_user_id=1, chat_id=1, kind="text", notion_token="x")


def test_session_roundtrip_and_expiry(store):
    now = datetime.now(UTC)
    store.save_session(7, '{"a":1}', now + timedelta(minutes=5))
    assert store.get_session(7, now) == '{"a":1}'
    assert store.get_session(7, now + timedelta(minutes=6)) is None
    assert store.get_session(7, now) is None  # expired row deleted


def test_session_replace_and_delete(store):
    now = datetime.now(UTC)
    store.save_session(7, "a", now + timedelta(minutes=5))
    store.save_session(7, "b", now + timedelta(minutes=5))
    assert store.get_session(7, now) == "b"
    store.delete_session(7)
    assert store.get_session(7, now) is None


def test_execution_undo_window(store):
    now = datetime.now(UTC)
    eid = store.new_event(telegram_user_id=1, chat_id=9, kind="text")
    xid = store.add_execution(eid, 9, 100, '{"kind":"archive","page_id":"p"}',
                              now + timedelta(minutes=5))
    assert store.get_execution(xid, now)["undo"] == '{"kind":"archive","page_id":"p"}'
    assert store.latest_execution(9, now)["id"] == xid
    assert store.get_execution(xid, now + timedelta(minutes=6)) is None
    store.mark_undone(xid)
    assert store.get_execution(xid, now)["undone"] == 1
    assert store.latest_execution(9, now) is None


def test_migrate_idempotent(tmp_path):
    p = tmp_path / "t.sqlite"
    AuditStore(p).migrate()
    s = AuditStore(p)
    s.migrate()
    s.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_audit_store.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `app/audit/store.py`** (and empty `__init__.py`)

```python
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  telegram_user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  kind TEXT NOT NULL,
  raw_input TEXT,
  transcription TEXT,
  llm_model TEXT,
  llm_context TEXT,
  llm_response TEXT,
  interpretation TEXT,
  candidate_scores TEXT,
  validation_result TEXT,
  decision TEXT,
  clarification_state TEXT,
  command TEXT,
  executed INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  error TEXT,
  duration_ms INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
  chat_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS executions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  chat_id INTEGER NOT NULL,
  reply_message_id INTEGER,
  undo TEXT NOT NULL,
  undone INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);
"""

EVENT_COLUMNS = frozenset(
    {"message_id", "kind", "raw_input", "transcription", "llm_model", "llm_context",
     "llm_response", "interpretation", "candidate_scores", "validation_result", "decision",
     "clarification_state", "command", "executed", "notion_page_id", "error", "duration_ms",
     "telegram_user_id", "chat_id"}
)


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


class AuditStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row

    def migrate(self) -> None:
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # events
    def new_event(self, *, telegram_user_id: int, chat_id: int, kind: str, **cols) -> int:
        cols.update(telegram_user_id=telegram_user_id, chat_id=chat_id, kind=kind)
        self._check_cols(cols)
        cols["ts"] = _iso(datetime.now(UTC))
        keys = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        cur = self._conn.execute(f"INSERT INTO events ({keys}) VALUES ({marks})", list(cols.values()))
        self._conn.commit()
        return int(cur.lastrowid)

    def update_event(self, event_id: int, **cols) -> None:
        self._check_cols(cols)
        if not cols:
            return
        sets = ", ".join(f"{k} = ?" for k in cols)
        self._conn.execute(f"UPDATE events SET {sets} WHERE id = ?", [*cols.values(), event_id])
        self._conn.commit()

    def get_event(self, event_id: int) -> dict | None:
        row = self._conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
        return dict(row) if row else None

    # sessions
    def save_session(self, chat_id: int, payload: str, expires_at: datetime) -> None:
        self._conn.execute(
            "INSERT INTO sessions (chat_id, payload, expires_at) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET payload = excluded.payload, "
            "expires_at = excluded.expires_at",
            (chat_id, payload, _iso(expires_at)),
        )
        self._conn.commit()

    def get_session(self, chat_id: int, now: datetime) -> str | None:
        row = self._conn.execute(
            "SELECT payload, expires_at FROM sessions WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            return None
        if row["expires_at"] <= _iso(now):
            self.delete_session(chat_id)
            return None
        return row["payload"]

    def delete_session(self, chat_id: int) -> None:
        self._conn.execute("DELETE FROM sessions WHERE chat_id = ?", (chat_id,))
        self._conn.commit()

    # executions
    def add_execution(
        self, event_id: int, chat_id: int, reply_message_id: int | None, undo: str,
        expires_at: datetime,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO executions (event_id, chat_id, reply_message_id, undo, expires_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (event_id, chat_id, reply_message_id, undo, _iso(expires_at)),
        )
        self._conn.commit()
        return int(cur.lastrowid)

    def get_execution(self, execution_id: int, now: datetime) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM executions WHERE id = ? AND expires_at > ?",
            (execution_id, _iso(now)),
        ).fetchone()
        return dict(row) if row else None

    def latest_execution(self, chat_id: int, now: datetime) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM executions WHERE chat_id = ? AND undone = 0 AND expires_at > ? "
            "ORDER BY id DESC LIMIT 1",
            (chat_id, _iso(now)),
        ).fetchone()
        return dict(row) if row else None

    def mark_undone(self, execution_id: int) -> None:
        self._conn.execute("UPDATE executions SET undone = 1 WHERE id = ?", (execution_id,))
        self._conn.commit()

    @staticmethod
    def _check_cols(cols: dict) -> None:
        bad = set(cols) - EVENT_COLUMNS
        if bad:
            raise ValueError(f"unknown event columns: {sorted(bad)}")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_audit_store.py -q`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/audit tests/test_audit_store.py
git commit -m "feat: sqlite audit store with sessions and undo records

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 6: Notion errors, provider protocol, property helpers

**Files:**
- Create: `app/notion/errors.py`, `app/notion/provider.py`, `app/notion/props.py`, `tests/test_props.py`

**Interfaces:**
- Produces:
  - `class NotionError(Exception)` with `status: int`, `code: str`, `message: str`; `NotionUnavailable(NotionError)` for network/5xx exhaustion.
  - `class NotionProvider(Protocol)` async methods: `me() -> dict`, `search(query: str | None = None, object_type: str | None = None) -> list[dict]` (all pages collected), `get_database(database_id) -> dict`, `get_data_source(data_source_id) -> dict`, `query_data_source(data_source_id, *, filter: dict | None = None, sorts: list[dict] | None = None, page_size: int = 50) -> list[dict]` (first page only), `get_page(page_id) -> dict`, `create_page(parent: dict, properties: dict, children: list[dict] | None = None) -> dict`, `update_page(page_id, *, properties: dict | None = None, archived: bool | None = None) -> dict`, `append_blocks(block_id, children: list[dict]) -> dict`, `delete_block(block_id) -> dict`.
  - `props.plain_text(rich: list[dict]) -> str`, `props.page_title(page: dict) -> str`, `props.field_type(prop: dict) -> FieldType`, `props.item_hint(page: dict) -> str | None`.

- [ ] **Step 1: Write failing tests**

`tests/test_props.py`:
```python
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
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_props.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement**

`app/notion/errors.py`:
```python
class NotionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class NotionUnavailable(NotionError):
    def __init__(self, message: str = "Notion unreachable") -> None:
        super().__init__(0, "unavailable", message)
```

`app/notion/provider.py`:
```python
from typing import Protocol


class NotionProvider(Protocol):
    async def me(self) -> dict: ...

    async def search(self, query: str | None = None, object_type: str | None = None) -> list[dict]: ...

    async def get_database(self, database_id: str) -> dict: ...

    async def get_data_source(self, data_source_id: str) -> dict: ...

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 50,
    ) -> list[dict]: ...

    async def get_page(self, page_id: str) -> dict: ...

    async def create_page(
        self, parent: dict, properties: dict, children: list[dict] | None = None
    ) -> dict: ...

    async def update_page(
        self, page_id: str, *, properties: dict | None = None, archived: bool | None = None
    ) -> dict: ...

    async def append_blocks(self, block_id: str, children: list[dict]) -> dict: ...

    async def delete_block(self, block_id: str) -> dict: ...
```

`app/notion/props.py`:
```python
from app.notion.snapshot import WRITABLE_TYPES, FieldType

UNTITLED = "(без названия)"


def plain_text(rich: list[dict]) -> str:
    return "".join(r.get("plain_text", "") for r in rich)


def page_title(page: dict) -> str:
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "title":
            text = plain_text(prop.get("title", [])).strip()
            return text or UNTITLED
    return UNTITLED


def field_type(prop: dict) -> FieldType:
    t = prop.get("type", "")
    return t if t in WRITABLE_TYPES else "readonly"  # type: ignore[return-value]


def item_hint(page: dict) -> str | None:
    props = page.get("properties", {})
    for prop in props.values():
        if prop.get("type") == "status" and prop.get("status"):
            return prop["status"].get("name")
    for name, prop in props.items():
        if prop.get("type") == "checkbox" and prop.get("checkbox") is True:
            return name
    return None
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_props.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/notion tests/test_props.py
git commit -m "feat: notion provider protocol, errors, property helpers

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 7: DirectNotionProvider (httpx)

**Files:**
- Create: `app/notion/direct.py`, `tests/test_notion_direct.py`

**Interfaces:**
- Consumes: `NotionError`, `NotionUnavailable`, `NotionProvider`.
- Produces: `class DirectNotionProvider(token: str, version: str = "2025-09-03", *, transport: httpx.AsyncBaseTransport | None = None, timeout: float = 30.0, max_retries: int = 3)` implementing every `NotionProvider` method; `async aclose()`; async context manager.

- [ ] **Step 1: Write failing tests**

`tests/test_notion_direct.py`:
```python
import json

import httpx
import pytest

from app.notion.direct import DirectNotionProvider
from app.notion.errors import NotionError, NotionUnavailable


def make(handler, **kw):
    return DirectNotionProvider("secret", transport=httpx.MockTransport(handler), **kw)


async def test_headers_and_me():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(dict(req.headers))
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"object": "user", "id": "u1"})

    async with make(handler) as p:
        assert (await p.me())["id"] == "u1"
    assert seen["authorization"] == "Bearer secret"
    assert seen["notion-version"] == "2025-09-03"
    assert seen["url"] == "https://api.notion.com/v1/users/me"


async def test_search_paginates_and_filters():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        if body.get("start_cursor") is None:
            return httpx.Response(200, json={"results": [{"id": "a"}], "has_more": True,
                                             "next_cursor": "c2"})
        return httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False,
                                         "next_cursor": None})

    async with make(handler) as p:
        res = await p.search(object_type="data_source")
    assert [r["id"] for r in res] == ["a", "b"]
    assert calls[0]["filter"] == {"property": "object", "value": "data_source"}
    assert calls[0]["page_size"] == 100
    assert calls[1]["start_cursor"] == "c2"


async def test_query_data_source_uses_patch_and_body():
    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": [{"id": "p"}], "has_more": False})

    async with make(handler) as p:
        res = await p.query_data_source("ds1", sorts=[{"timestamp": "last_edited_time",
                                                       "direction": "descending"}], page_size=7)
    assert res == [{"id": "p"}]
    assert seen["method"] == "PATCH"
    assert seen["url"].endswith("/v1/data_sources/ds1/query")
    assert seen["body"] == {"sorts": [{"timestamp": "last_edited_time",
                                       "direction": "descending"}], "page_size": 7}


async def test_create_update_append_delete_shapes():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"id": "x"})

    async with make(handler) as p:
        await p.create_page({"type": "data_source_id", "data_source_id": "ds"}, {"T": {}},
                            children=[{"object": "block"}])
        await p.update_page("pg", properties={"A": {}})
        await p.update_page("pg", archived=True)
        await p.append_blocks("blk", [{"object": "block"}])
        await p.delete_block("blk")
        await p.get_database("db")
        await p.get_data_source("ds")
        await p.get_page("pg")
    assert seen[0] == ("POST", "/v1/pages", {"parent": {"type": "data_source_id",
                                                        "data_source_id": "ds"},
                                             "properties": {"T": {}},
                                             "children": [{"object": "block"}]})
    assert seen[1] == ("PATCH", "/v1/pages/pg", {"properties": {"A": {}}})
    assert seen[2] == ("PATCH", "/v1/pages/pg", {"archived": True})
    assert seen[3] == ("PATCH", "/v1/blocks/blk/children", {"children": [{"object": "block"}]})
    assert seen[4] == ("DELETE", "/v1/blocks/blk", None)
    assert seen[5] == ("GET", "/v1/databases/db", None)
    assert seen[6] == ("GET", "/v1/data_sources/ds", None)
    assert seen[7] == ("GET", "/v1/pages/pg", None)


async def test_4xx_raises_notion_error_without_token():
    def handler(req):
        return httpx.Response(400, json={"code": "validation_error", "message": "bad prop"})

    async with make(handler) as p:
        with pytest.raises(NotionError) as ei:
            await p.get_page("pg")
    assert ei.value.status == 400
    assert ei.value.code == "validation_error"
    assert "secret" not in str(ei.value)


async def test_429_retries_with_retry_after(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"},
                                  json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"id": "ok"})

    async with make(handler) as p:
        assert (await p.get_page("pg"))["id"] == "ok"
    assert sleeps == [2.0, 2.0]


async def test_429_exhausted_raises(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)

    def handler(req):
        return httpx.Response(429, json={"code": "rate_limited", "message": "slow"})

    async with make(handler, max_retries=2) as p:
        with pytest.raises(NotionError) as ei:
            await p.get_page("pg")
    assert ei.value.status == 429


async def test_5xx_and_network_become_unavailable(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)

    def handler(req):
        return httpx.Response(502, text="bad gateway")

    async with make(handler) as p:
        with pytest.raises(NotionUnavailable):
            await p.get_page("pg")

    def boom(req):
        raise httpx.ConnectError("no route")

    async with make(boom) as p:
        with pytest.raises(NotionUnavailable):
            await p.get_page("pg")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_notion_direct.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `app/notion/direct.py`**

```python
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.notion.errors import NotionError, NotionUnavailable

log = logging.getLogger(__name__)
BASE_URL = "https://api.notion.com/v1"


class DirectNotionProvider:
    def __init__(
        self,
        token: str,
        version: str = "2025-09-03",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DirectNotionProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- low level -------------------------------------------------------

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        attempt = 0
        while True:
            try:
                resp = await self._client.request(method, path, json=json)
            except httpx.HTTPError as e:
                if attempt >= self._max_retries:
                    raise NotionUnavailable(f"network error: {type(e).__name__}") from None
                attempt += 1
                await asyncio.sleep(min(2.0**attempt, 8.0))
                continue

            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            if resp.status_code == 429 and attempt < self._max_retries:
                attempt += 1
                delay = float(resp.headers.get("Retry-After", "1"))
                log.warning("notion 429, retry %d in %.1fs", attempt, delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 500:
                if attempt < min(self._max_retries, 2):
                    attempt += 1
                    await asyncio.sleep(min(2.0**attempt, 8.0))
                    continue
                raise NotionUnavailable(f"server error {resp.status_code}")

            code, message = self._error_parts(resp)
            raise NotionError(resp.status_code, code, message)

    @staticmethod
    def _error_parts(resp: httpx.Response) -> tuple[str, str]:
        try:
            body: Any = resp.json()
            return str(body.get("code", "error")), str(body.get("message", ""))
        except ValueError:
            return "error", resp.text[:200]

    async def _paginate(self, method: str, path: str, body: dict) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            payload = {**body, "page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = await self._request(method, path, payload)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")
            if not cursor:
                return results

    # ---- API -------------------------------------------------------------

    async def me(self) -> dict:
        return await self._request("GET", "/users/me")

    async def search(self, query: str | None = None, object_type: str | None = None) -> list[dict]:
        body: dict = {}
        if query:
            body["query"] = query
        if object_type:
            body["filter"] = {"property": "object", "value": object_type}
        return await self._paginate("POST", "/search", body)

    async def get_database(self, database_id: str) -> dict:
        return await self._request("GET", f"/databases/{database_id}")

    async def get_data_source(self, data_source_id: str) -> dict:
        return await self._request("GET", f"/data_sources/{data_source_id}")

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 50,
    ) -> list[dict]:
        body: dict = {"page_size": page_size}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        data = await self._request("PATCH", f"/data_sources/{data_source_id}/query", body)
        return data.get("results", [])

    async def get_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{page_id}")

    async def create_page(
        self, parent: dict, properties: dict, children: list[dict] | None = None
    ) -> dict:
        body: dict = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return await self._request("POST", "/pages", body)

    async def update_page(
        self, page_id: str, *, properties: dict | None = None, archived: bool | None = None
    ) -> dict:
        body: dict = {}
        if properties is not None:
            body["properties"] = properties
        if archived is not None:
            body["archived"] = archived
        return await self._request("PATCH", f"/pages/{page_id}", body)

    async def append_blocks(self, block_id: str, children: list[dict]) -> dict:
        return await self._request("PATCH", f"/blocks/{block_id}/children", {"children": children})

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"/blocks/{block_id}")
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_notion_direct.py -q`
Expected: 8 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/notion/direct.py tests/test_notion_direct.py
git commit -m "feat: direct Notion REST provider with retries

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 8: Descriptions file (targets.yaml)

**Files:**
- Create: `app/notion/descriptions.py`, `tests/test_descriptions.py`

**Interfaces:**
- Produces: pydantic models `FieldMeta(name: str = "", description: str = "", required: bool = False)`, `TargetMeta(name: str = "", description: str = "", fields: dict[str, FieldMeta] = {})`; `class Descriptions(path: Path)` with `load() -> dict[str, TargetMeta]`, `save(meta: dict[str, TargetMeta])`, `ensure(discovered: dict[str, tuple[str, dict[str, str]]]) -> dict[str, TargetMeta]` where `discovered` maps target id → (name, {field_id: field_name}); `ensure` adds missing ids/fields, refreshes names, keeps descriptions/required, writes file if changed, returns merged meta.

- [ ] **Step 1: Write failing tests**

`tests/test_descriptions.py`:
```python
import yaml

from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta


def test_load_missing_file_is_empty(tmp_path):
    assert Descriptions(tmp_path / "t.yaml").load() == {}


def test_ensure_adds_and_preserves(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old", description="keep me",
                              fields={"f1": FieldMeta(name="X", description="d", required=True)})})
    merged = d.ensure({"ds1": ("Покупки", {"f1": "Название", "f2": "Магазин"}),
                       "pg1": ("Идеи", {})})
    assert merged["ds1"].name == "Покупки"
    assert merged["ds1"].description == "keep me"
    assert merged["ds1"].fields["f1"].required is True
    assert merged["ds1"].fields["f1"].name == "Название"
    assert merged["ds1"].fields["f2"] == FieldMeta(name="Магазин")
    assert merged["pg1"] == TargetMeta(name="Идеи")
    on_disk = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert on_disk["pg1"]["name"] == "Идеи"
    assert on_disk["ds1"]["fields"]["f1"]["required"] is True


def test_ensure_never_deletes(tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"gone": TargetMeta(name="Gone", description="x")})
    merged = d.ensure({})
    assert merged["gone"].description == "x"


def test_utf8_roundtrip(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"a": TargetMeta(name="Ёжик", description="описание")})
    assert "Ёжик" in p.read_text(encoding="utf-8")
    assert d.load()["a"].description == "описание"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_descriptions.py -q`
Expected: ModuleNotFoundError.

- [ ] **Step 3: Implement `app/notion/descriptions.py`**

```python
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class FieldMeta(BaseModel):
    name: str = ""
    description: str = ""
    required: bool = False


class TargetMeta(BaseModel):
    name: str = ""
    description: str = ""
    fields: dict[str, FieldMeta] = Field(default_factory=dict)


class Descriptions:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, TargetMeta]:
        if not self._path.exists():
            return {}
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        return {str(k): TargetMeta.model_validate(v or {}) for k, v in raw.items()}

    def save(self, meta: dict[str, TargetMeta]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump() for k, v in meta.items()}
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def ensure(self, discovered: dict[str, tuple[str, dict[str, str]]]) -> dict[str, TargetMeta]:
        meta = self.load()
        before = {k: v.model_dump() for k, v in meta.items()}
        for tid, (name, fields) in discovered.items():
            t = meta.setdefault(tid, TargetMeta())
            t.name = name
            for fid, fname in fields.items():
                f = t.fields.setdefault(fid, FieldMeta())
                f.name = fname
        if {k: v.model_dump() for k, v in meta.items()} != before:
            self.save(meta)
        return meta
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_descriptions.py -q`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```powershell
git add app/notion/descriptions.py tests/test_descriptions.py
git commit -m "feat: targets.yaml descriptions store

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 9: Discovery → WorkspaceSnapshot

**Files:**
- Create: `app/notion/discovery.py`, `tests/test_discovery.py`, `tests/fakes.py`

**Interfaces:**
- Consumes: `NotionProvider`, `Descriptions`, snapshot dataclasses, `props` helpers.
- Produces: `class Discovery(provider: NotionProvider, descriptions: Descriptions, *, items_per_target: int = 50, ttl_s: int = 60, clock: Callable[[], datetime] = now_utc)` with `async refresh() -> WorkspaceSnapshot`, `async get() -> WorkspaceSnapshot` (cached within ttl; on failure returns stale snapshot < 1 h old and logs, else raises), `invalidate()`, property `last: WorkspaceSnapshot | None`. Also `tests/fakes.py: FakeNotionProvider` reused by later plans.

Notion object shapes used (2025-09-03):
- search result page: `{"object":"page","id","url","last_edited_time","parent":{"type":"workspace"|"page_id"|"data_source_id"|"database_id", ...},"properties":{...}}`
- search result data source: `{"object":"data_source","id","parent":{"type":"database_id","database_id"}}`
- data source: `{"id","title":[rich],"description":[rich],"url","parent":{"database_id"},"properties":{name:{"id","type",...,"select":{"options":[{"id","name"}]},"status":{"options":[...]},"relation":{"data_source_id"}}}}`
- database: `{"id","parent":{"type":"page_id"|"workspace",...}}`

- [ ] **Step 1: Write fake provider**

`tests/fakes.py`:
```python
from __future__ import annotations

from app.notion.errors import NotionError


class FakeNotionProvider:
    """In-memory Notion. Feed it search results, data sources, databases, and pages per ds."""

    def __init__(self) -> None:
        self.search_results: list[dict] = []
        self.data_sources: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.calls: list[tuple] = []
        self.fail_search: Exception | None = None

    async def me(self) -> dict:
        return {"object": "user", "id": "bot"}

    async def search(self, query=None, object_type=None) -> list[dict]:
        self.calls.append(("search", query, object_type))
        if self.fail_search:
            raise self.fail_search
        res = self.search_results
        if object_type:
            res = [r for r in res if r["object"] == object_type]
        return list(res)

    async def get_database(self, database_id: str) -> dict:
        self.calls.append(("get_database", database_id))
        if database_id not in self.databases:
            raise NotionError(404, "object_not_found", database_id)
        return self.databases[database_id]

    async def get_data_source(self, data_source_id: str) -> dict:
        self.calls.append(("get_data_source", data_source_id))
        if data_source_id not in self.data_sources:
            raise NotionError(404, "object_not_found", data_source_id)
        return self.data_sources[data_source_id]

    async def query_data_source(self, data_source_id, *, filter=None, sorts=None, page_size=50):
        self.calls.append(("query", data_source_id, page_size))
        if data_source_id not in self.data_sources:
            raise NotionError(404, "object_not_found", data_source_id)
        return list(self.items.get(data_source_id, []))[:page_size]

    async def get_page(self, page_id: str) -> dict:
        self.calls.append(("get_page", page_id))
        return {"id": page_id}

    async def create_page(self, parent, properties, children=None) -> dict:
        self.calls.append(("create_page", parent, properties, children))
        return {"id": "new-page", "url": "https://notion.so/new-page", "properties": properties}

    async def update_page(self, page_id, *, properties=None, archived=None) -> dict:
        self.calls.append(("update_page", page_id, properties, archived))
        return {"id": page_id, "properties": properties or {}, "archived": bool(archived)}

    async def append_blocks(self, block_id, children) -> dict:
        self.calls.append(("append_blocks", block_id, children))
        return {"results": [{"id": f"blk-{i}"} for i, _ in enumerate(children)]}

    async def delete_block(self, block_id) -> dict:
        self.calls.append(("delete_block", block_id))
        return {"id": block_id, "archived": True}
```

- [ ] **Step 2: Write failing tests**

`tests/test_discovery.py`:
```python
from datetime import UTC, datetime, timedelta

import pytest

from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta
from app.notion.discovery import Discovery
from app.notion.errors import NotionUnavailable
from tests.fakes import FakeNotionProvider


def rich(text):
    return [{"plain_text": text}]


def page(id, title, parent, edited="2026-09-01T00:00:00.000Z", extra=None):
    props = {"title": {"id": "title", "type": "title", "title": rich(title)}}
    props.update(extra or {})
    return {"object": "page", "id": id, "url": f"https://notion.so/{id}",
            "last_edited_time": edited, "parent": parent, "properties": props}


@pytest.fixture
def fake():
    f = FakeNotionProvider()
    f.search_results = [
        page("home", "Дом", {"type": "workspace", "workspace": True}),
        page("ideas", "Идеи", {"type": "page_id", "page_id": "home"}),
        page("trip", "Отпуск", {"type": "page_id", "page_id": "ideas"}),
        {"object": "data_source", "id": "ds-buy",
         "parent": {"type": "database_id", "database_id": "db-buy"}},
        {"object": "data_source", "id": "ds-shops",
         "parent": {"type": "database_id", "database_id": "db-shops"}},
        page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
    ]
    f.databases = {
        "db-buy": {"id": "db-buy", "parent": {"type": "page_id", "page_id": "home"}},
        "db-shops": {"id": "db-shops", "parent": {"type": "workspace", "workspace": True}},
    }
    f.data_sources = {
        "ds-buy": {
            "id": "ds-buy", "title": rich("Покупки"), "description": rich("Из Notion"),
            "url": "https://notion.so/ds-buy", "parent": {"database_id": "db-buy"},
            "properties": {
                "Название": {"id": "title", "type": "title"},
                "Магазин": {"id": "shop", "type": "select",
                            "select": {"options": [{"id": "o1", "name": "Rimi"},
                                                   {"id": "o2", "name": "Prisma"}]}},
                "Куплено": {"id": "done", "type": "checkbox"},
                "Где": {"id": "rel", "type": "relation", "relation": {"data_source_id": "ds-shops"}},
                "Формула": {"id": "fx", "type": "formula"},
            },
        },
        "ds-shops": {
            "id": "ds-shops", "title": rich("Магазины"), "description": [],
            "url": "https://notion.so/ds-shops", "parent": {"database_id": "db-shops"},
            "properties": {"Name": {"id": "title", "type": "title"}},
        },
    }
    f.items = {
        "ds-buy": [
            page("row2", "Молоко", {"type": "data_source_id", "data_source_id": "ds-buy"},
                 edited="2026-09-02T00:00:00.000Z",
                 extra={"Куплено": {"type": "checkbox", "checkbox": True}}),
            page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
        ],
        "ds-shops": [page("s1", "Rimi Hyper", {"type": "data_source_id",
                                               "data_source_id": "ds-shops"})],
    }
    return f


@pytest.fixture
def disco(fake, tmp_path):
    return Discovery(fake, Descriptions(tmp_path / "t.yaml"), items_per_target=10, ttl_s=60)


async def test_databases_discovered(disco):
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.kind == "database"
    assert buy.name == "Покупки"
    assert buy.path == "Дом / Покупки"
    assert buy.database_id == "db-buy"
    assert buy.parent_page_id == "home"
    assert buy.description == "Из Notion"
    assert {f.id: f.type for f in buy.fields} == {"title": "title", "shop": "select",
                                                  "done": "checkbox", "rel": "relation",
                                                  "fx": "readonly"}
    assert buy.field("title").required is True
    assert [o.name for o in buy.field("shop").options] == ["Rimi", "Prisma"]
    assert buy.field("rel").relation_data_source_id == "ds-shops"
    assert [o.name for o in buy.field("rel").options] == ["Rimi Hyper"]
    assert [(i.id, i.title, i.hint) for i in buy.items] == [("row2", "Молоко", "Куплено"),
                                                            ("row1", "Хлеб", None)]
    assert buy.operations == frozenset({"create", "update", "search"})
    assert snap.target("ds-shops").path == "Магазины"


async def test_pages_discovered_with_children(disco):
    snap = await disco.refresh()
    ideas = snap.target("ideas")
    assert ideas.kind == "page"
    assert ideas.path == "Дом / Идеи"
    assert ideas.parent_page_id == "home"
    assert [i.title for i in ideas.items] == ["Отпуск"]
    assert ideas.operations == frozenset({"create_page", "append", "search"})
    assert snap.target("row1") is None  # rows are not page targets
    assert snap.target("home").path == "Дом"


async def test_descriptions_override_and_required(disco, tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(description="Мой список",
                                 fields={"shop": FieldMeta(description="Сеть", required=True)})})
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.description == "Мой список"
    assert buy.field("shop").required is True
    assert buy.field("shop").description == "Сеть"
    on_disk = d.load()
    assert on_disk["ideas"].name == "Идеи"
    assert on_disk["ds-buy"].fields["done"].name == "Куплено"


async def test_cache_ttl_and_invalidate(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    b = await disco.get()
    assert a is b
    t["now"] += timedelta(seconds=61)
    c = await disco.get()
    assert c is not a
    disco.invalidate()
    assert (await disco.get()) is not c
    assert sum(1 for c in fake.calls if c[0] == "search") == 3


async def test_stale_snapshot_on_failure(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    fake.fail_search = NotionUnavailable()
    t["now"] += timedelta(minutes=30)
    assert (await disco.get()) is a
    t["now"] += timedelta(minutes=31)
    with pytest.raises(NotionUnavailable):
        await disco.get()


async def test_missing_relation_target_gives_empty_options(disco, fake):
    del fake.data_sources["ds-shops"]
    fake.search_results = [r for r in fake.search_results if r["id"] != "ds-shops"]
    snap = await disco.refresh()
    assert snap.target("ds-buy").field("rel").options == []
```

- [ ] **Step 3: Run to verify failure**

Run: `uv run pytest tests/test_discovery.py -q`
Expected: ModuleNotFoundError `app.notion.discovery`.

- [ ] **Step 4: Implement `app/notion/discovery.py`**

```python
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.notion import props
from app.notion.descriptions import Descriptions, TargetMeta
from app.notion.errors import NotionError
from app.notion.provider import NotionProvider
from app.notion.snapshot import (
    DB_OPERATIONS,
    PAGE_OPERATIONS,
    Field,
    Item,
    Option,
    Target,
    WorkspaceSnapshot,
)

log = logging.getLogger(__name__)
STALE_MAX = timedelta(hours=1)
CONCURRENCY = 3


def now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_time(s: str | None) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Discovery:
    def __init__(
        self,
        provider: NotionProvider,
        descriptions: Descriptions,
        *,
        items_per_target: int = 50,
        ttl_s: int = 60,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._p = provider
        self._desc = descriptions
        self._items_per_target = items_per_target
        self._ttl = timedelta(seconds=ttl_s)
        self._clock = clock
        self._last: WorkspaceSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def last(self) -> WorkspaceSnapshot | None:
        return self._last

    def invalidate(self) -> None:
        self._last = None

    async def get(self) -> WorkspaceSnapshot:
        async with self._lock:
            now = self._clock()
            if self._last and now - self._last.fetched_at < self._ttl:
                return self._last
            try:
                return await self._refresh_locked()
            except NotionError as e:
                if self._last and now - self._last.fetched_at < STALE_MAX:
                    log.warning("discovery failed (%s); using stale snapshot", e.code)
                    return self._last
                raise

    async def refresh(self) -> WorkspaceSnapshot:
        async with self._lock:
            return await self._refresh_locked()

    # ---- internals -------------------------------------------------------

    async def _refresh_locked(self) -> WorkspaceSnapshot:
        results = await self._p.search()
        pages = [r for r in results if r.get("object") == "page"]
        ds_ids = [r["id"] for r in results if r.get("object") == "data_source"]

        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch_ds(ds_id: str) -> tuple[dict, list[dict]]:
            async with sem:
                ds = await self._p.get_data_source(ds_id)
                items = await self._p.query_data_source(
                    ds_id,
                    sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
                    page_size=self._items_per_target,
                )
            return ds, items

        fetched = await asyncio.gather(*(fetch_ds(i) for i in ds_ids))
        sources = {ds["id"]: ds for ds, _ in fetched}
        items_by_ds = {ds["id"]: rows for ds, rows in fetched}

        db_ids = {ds.get("parent", {}).get("database_id") for ds in sources.values()}
        db_ids.discard(None)

        async def fetch_db(db_id: str) -> tuple[str, dict]:
            async with sem:
                try:
                    return db_id, await self._p.get_database(db_id)
                except NotionError as e:
                    log.warning("database %s not readable: %s", db_id, e.code)
                    return db_id, {}

        databases = dict(await asyncio.gather(*(fetch_db(i) for i in db_ids)))

        # relation targets not visible via search: try one query, else empty
        for ds in sources.values():
            for prop in ds.get("properties", {}).values():
                rel = prop.get("relation", {}).get("data_source_id") if prop.get("type") == "relation" else None
                if rel and rel not in items_by_ds:
                    try:
                        items_by_ds[rel] = await self._p.query_data_source(
                            rel, page_size=self._items_per_target
                        )
                    except NotionError:
                        items_by_ds[rel] = []

        top_pages = [p for p in pages if p.get("parent", {}).get("type") in ("workspace", "page_id")]
        page_by_id = {p["id"]: p for p in top_pages}
        titles = {pid: props.page_title(p) for pid, p in page_by_id.items()}
        for db_id, db in databases.items():
            titles[db_id] = ""  # databases are transparent in paths
        for ds in sources.values():
            titles[ds["id"]] = props.plain_text(ds.get("title", []))

        def parent_page_of(parent: dict) -> str | None:
            t = parent.get("type")
            if t == "page_id":
                return parent.get("page_id")
            if t == "database_id":
                db = databases.get(parent["database_id"], {})
                return parent_page_of(db.get("parent", {})) if db else None
            return None

        def path_of(name: str, parent_page_id: str | None) -> str:
            chain = [name]
            seen = set()
            pid = parent_page_id
            while pid and pid in page_by_id and pid not in seen:
                seen.add(pid)
                chain.append(titles[pid])
                pid = parent_page_of(page_by_id[pid].get("parent", {}))
            return " / ".join(reversed(chain))

        discovered: dict[str, tuple[str, dict[str, str]]] = {}
        targets: list[Target] = []

        for ds_id, ds in sources.items():
            name = titles[ds_id] or props.UNTITLED
            db_id = ds.get("parent", {}).get("database_id")
            parent_page = parent_page_of(databases.get(db_id, {}).get("parent", {})) if db_id else None
            field_names: dict[str, str] = {}
            fields: list[Field] = []
            for pname, prop in ds.get("properties", {}).items():
                ftype = props.field_type(prop)
                options: list[Option] = []
                rel_ds = None
                if ftype in ("select", "multi_select", "status"):
                    options = [Option(o["id"], o["name"]) for o in prop.get(ftype, {}).get("options", [])]
                elif ftype == "relation":
                    rel_ds = prop.get("relation", {}).get("data_source_id")
                    options = [Option(r["id"], props.page_title(r)) for r in items_by_ds.get(rel_ds, [])]
                fields.append(Field(id=prop["id"], name=pname, type=ftype, required=ftype == "title",
                                    options=options, relation_data_source_id=rel_ds, description=""))
                field_names[prop["id"]] = pname
            items = [
                Item(id=r["id"], title=props.page_title(r), hint=props.item_hint(r),
                     last_edited=_parse_time(r.get("last_edited_time")))
                for r in items_by_ds.get(ds_id, [])
            ]
            discovered[ds_id] = (name, field_names)
            targets.append(Target(
                id=ds_id, kind="database", name=name, path=path_of(name, parent_page),
                description=props.plain_text(ds.get("description", [])),
                parent_page_id=parent_page, database_id=db_id, fields=fields, items=items,
                operations=DB_OPERATIONS, url=ds.get("url", ""),
            ))

        children: dict[str, list[dict]] = {}
        for p in top_pages:
            pp = parent_page_of(p.get("parent", {}))
            if pp:
                children.setdefault(pp, []).append(p)

        for pid, p in page_by_id.items():
            name = titles[pid]
            parent_page = parent_page_of(p.get("parent", {}))
            kids = sorted(children.get(pid, []), key=lambda c: c.get("last_edited_time", ""), reverse=True)
            items = [Item(id=c["id"], title=titles[c["id"]], hint=None,
                          last_edited=_parse_time(c.get("last_edited_time")))
                     for c in kids[: self._items_per_target]]
            discovered[pid] = (name, {})
            targets.append(Target(
                id=pid, kind="page", name=name, path=path_of(name, parent_page), description="",
                parent_page_id=parent_page, database_id=None, fields=[], items=items,
                operations=PAGE_OPERATIONS, url=p.get("url", ""),
            ))

        meta = self._desc.ensure(discovered)
        targets = [self._apply_meta(t, meta.get(t.id)) for t in targets]
        targets.sort(key=lambda t: t.path)
        self._last = WorkspaceSnapshot(fetched_at=self._clock(), targets=targets)
        log.info("discovered %d targets", len(targets))
        return self._last

    @staticmethod
    def _apply_meta(t: Target, m: TargetMeta | None) -> Target:
        if m is None:
            return t
        fields = []
        for f in t.fields:
            fm = m.fields.get(f.id)
            if fm is None:
                fields.append(f)
                continue
            fields.append(Field(
                id=f.id, name=f.name, type=f.type,
                required=f.required or fm.required,
                options=f.options, relation_data_source_id=f.relation_data_source_id,
                description=fm.description,
            ))
        return Target(
            id=t.id, kind=t.kind, name=t.name, path=t.path,
            description=m.description or t.description,
            parent_page_id=t.parent_page_id, database_id=t.database_id, fields=fields,
            items=t.items, operations=t.operations, url=t.url,
        )
```

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/test_discovery.py -q`
Expected: 6 passed. If `test_stale_snapshot_on_failure` fails because `NotionUnavailable` is raised from `search` before `_refresh_locked` catches it: `NotionUnavailable` subclasses `NotionError`, so the `except NotionError` in `get()` covers it; check the `fail_search` wiring in the fake.

- [ ] **Step 6: Run full suite and lint**

```powershell
uv run pytest -q
uv run ruff check .
```
Expected: all pass. Fix any `E501` by wrapping lines.

- [ ] **Step 7: Commit**

```powershell
git add app/notion/discovery.py tests/test_discovery.py tests/fakes.py
git commit -m "feat: workspace discovery into snapshot with TTL cache

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

### Task 10: `tools/discover.py` CLI and real-API smoke

**Files:**
- Create: `tools/__init__.py`, `tools/discover.py`
- Modify: `README.md` (already references the tool; verify text)

**Interfaces:**
- Consumes: `load_settings`, `DirectNotionProvider`, `Descriptions`, `Discovery`.
- Produces: `python -m tools.discover` prints the target tree; exit 3 on 401.

- [ ] **Step 1: Implement `tools/discover.py`** (and empty `tools/__init__.py`)

```python
"""Print what the Notion integration can see, as the bot will see it."""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import load_settings
from app.notion.descriptions import Descriptions
from app.notion.direct import DirectNotionProvider
from app.notion.discovery import Discovery
from app.notion.errors import NotionError


async def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    s = load_settings()
    async with DirectNotionProvider(s.notion_token, s.notion_version) as p:
        try:
            await p.me()
        except NotionError as e:
            print(f"Notion auth failed: {e.status} {e.code}", file=sys.stderr)
            return 3
        disco = Discovery(p, Descriptions(s.targets_file), items_per_target=s.items_per_target)
        snap = await disco.refresh()

    print(f"{len(snap.targets)} targets (descriptions in {s.targets_file}):\n")
    for t in snap.targets:
        kind = "DB " if t.kind == "database" else "PG "
        print(f"{kind}{t.path}   [{t.id}]")
        if t.description:
            print(f"     ~ {t.description}")
        for f in t.fields:
            opts = f" {{{', '.join(o.name for o in f.options[:8])}}}" if f.options else ""
            req = "*" if f.required else ""
            print(f"     - {f.name}{req}: {f.type}{opts}")
        for i in t.items[:10]:
            hint = f" ({i.hint})" if i.hint else ""
            print(f"       • {i.title}{hint}")
        if len(t.items) > 10:
            print(f"       … +{len(t.items) - 10}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 2: Lint**

Run: `uv run ruff check .`
Expected: pass.

- [ ] **Step 3: Real-API smoke (only if `.env` has `NOTION_TOKEN`; otherwise note in commit that it is untested against the live API)**

Run: `uv run python -m tools.discover`
Expected: tree of your shared pages and databases; `data/targets.yaml` created.

- [ ] **Step 4: Commit**

```powershell
git add tools
git commit -m "feat: discover CLI to print workspace snapshot

Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage (T-001…T-015):** T-001 → Task 1; T-002 (Ollama install + pulls) is environment work with no code, deferred to Plan 2 Task 1; T-003 → Task 1 (.env.example, .gitignore, README); T-004 → Task 2; T-010 → Tasks 3, 4; T-011 → Task 5; T-012 (`texts.py`, keyboards) moved to Plan 3 where they are consumed; T-013 → Tasks 6, 7; T-014 → Task 8; T-015 → Tasks 9, 10.

**Type consistency:** `Field.required` is set `ftype == "title"` in discovery and OR-ed with `FieldMeta.required`; `Descriptions.ensure` signature `dict[str, tuple[str, dict[str, str]]]` matches the discovery call; `FakeNotionProvider` method signatures mirror `NotionProvider`; `AuditStore` column set matches DATA_MODEL §6.

**Known simplifications:** page targets do not include their body blocks; databases inside a data-source row (nested DBs) are discovered through search like any other data source; `Item.hint` is only status name or a true checkbox name.
