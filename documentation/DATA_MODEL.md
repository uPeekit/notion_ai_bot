# Data Model

## 1. Workspace snapshot (in-memory, rebuilt from Notion)

Per-request short keys live in the ContextBuilder (Plan 2), not on the shared cached snapshot.

```python
@dataclass(frozen=True)
class WorkspaceSnapshot:
    fetched_at: datetime
    targets: list[Target]               # databases (data sources) and pages

@dataclass(frozen=True)
class Target:
    id: str                             # data_source_id or page_id
    kind: Literal["database", "page"]
    name: str
    path: str                           # "Дом / Покупки" (parent chain titles)
    description: str                    # targets.yaml override → Notion description → ""
    parent_page_id: str | None
    database_id: str | None             # for kind=database
    fields: list[Field]                 # empty for pages
    items: list[Item]                   # database rows or child pages, newest first, capped
    operations: frozenset[str]          # database: create, update, search; page: create_page, append, search

@dataclass(frozen=True)
class Field:
    id: str                             # Notion property id
    name: str
    type: Literal["title","rich_text","select","multi_select","status","date",
                  "checkbox","number","url","relation","readonly"]
    required: bool                      # title=True; others from targets.yaml
    options: list[Option]               # select/multi_select/status; relation → related items
    relation_data_source_id: str | None
    description: str                    # from targets.yaml

@dataclass(frozen=True)
class Option:
    id: str                             # option id or related page id
    name: str

@dataclass(frozen=True)
class Item:
    id: str                             # page id
    title: str
    hint: str | None                    # e.g. status name or "куплено", shown to LLM in parentheses
    last_edited: datetime
    url: str                            # Notion page url
```

## 2. targets.yaml (human-edited, `data/targets.yaml`)

```yaml
# keyed by Notion id (data source or page id), auto-populated on discovery
"2f1c…a9":
  name: Покупки            # read-only, written by discovery for readability
  description: Список покупок для дома. Каждая строка — один товар.
  fields:
    "%3Ek%7C":              # property id
      name: Магазин
      description: Сеть магазинов, куда идти
      required: false
"7ab0…33":
  name: Идеи
  description: Свободные заметки и идеи, дописывать абзацами.
```

Discovery adds missing ids with empty descriptions and never deletes entries.

## 3. LLM interpretation (Pydantic, `interpretation/models.py`)

```python
class Scored(BaseModel):
    confidence: float = Field(ge=0, le=1)

class Intent(Scored):
    value: Literal["create", "update", "append", "search", "unknown"]

class NotMentioned(BaseModel):  status: Literal["not_mentioned"]
class ExplicitNull(BaseModel):  status: Literal["explicit_null"]
class Ambiguous(BaseModel):
    status: Literal["ambiguous"]; candidates: list[Any]; source_text: str
class Value(Scored):
    status: Literal["value"]; value: Any; source_text: str

FieldValue = Annotated[NotMentioned | ExplicitNull | Ambiguous | Value, Discriminator("status")]

class DateValue(BaseModel):
    start: str                          # ISO date or datetime
    end: str | None = None

class Candidate(Scored):
    target: str                         # key
    item: str | None = None             # key
    item_candidates: list[str] = []
    fields: dict[str, FieldValue]       # key → value; every writable field present
    content: str | None = None
    search_query: str | None = None

class Interpretation(BaseModel):
    intent: Intent
    candidates: list[Candidate] = Field(min_length=1, max_length=3)
    notes: str = ""
```

The JSON schema sent to Ollama is generated from these models and then specialised per request: `target`, `item`, `item_candidates[]`, field keys and option values become `enum`s from the current context. Typed `value` per field type replaces `Any`.

## 4. Resolved interpretation (after clarification)

`Question`/`QOption`/`Decision` are real code (`validation/policy.py`), produced by `Policy.evaluate(ValidationResult) -> Decision`:

```python
QType = Literal["target", "item", "item_not_found", "field_required", "field_ambiguous",
                "field_confirm", "date", "content_required"]

@dataclass(frozen=True)
class QOption:
    key: str                            # format depends on question type, see table below
    label: str

@dataclass
class Question:
    type: QType
    target_key: str | None = None       # candidate key the question concerns, e.g. "t3"
    field_key: str | None = None        # set for field_required / field_ambiguous / field_confirm / date
    field_name: str | None = None
    options: list[QOption] = []         # empty for field_confirm, date, item_not_found, content_required
    proposed: Any = None                # date / field_confirm: the low-confidence typed value to confirm

@dataclass
class Decision:
    kind: Literal["EXECUTE", "CLARIFY", "REJECT"]
    candidate: VCandidate | None         # set for EXECUTE, CLARIFY, and the item_not_found REJECT; else None
    questions: list[Question]            # non-empty for CLARIFY; exactly one Question("item_not_found", ...) for that REJECT case; else []
    reasons: list[str]                   # REJECT: issue codes/messages, ["no valid candidates"], or ["item not found in target"] for the item_not_found case; CLARIFY: question types in ask order
    risk: Literal["LOW", "MEDIUM"] | None  # RISK_BY_INTENT[intent]; None only when REJECT has no candidate
```

`QOption.key` format by question type (built in `policy.py`):

| Question type | Key format | Example |
|---|---|---|
| `target` | candidate key (the target key itself) | `t3` |
| `item` | `<target_key>.item:<page id>` | `t3.item:2f1c…a9` |
| `field_required`, when field has non-empty `options` | `<field_key>.o<1-based index>` | `t3.f2.o1` |
| `field_ambiguous` | `<field_key>#<0-based index>` | `t3.f2#0` |
| `field_confirm`, `date`, `item_not_found`, `content_required` | no options; answer is free text or a confirm/other action | — |

Question order (ties within one `Decision.questions` broken by this order, one asked at a time): `target → item → item_not_found → field_required → field_ambiguous → date → field_confirm → content_required`.

Special case: `update` intent with no resolvable item and no `item_candidates` does not ask a question — it returns `Decision("REJECT", best, [Question("item_not_found", ...)], ["item not found in target"], risk)`. The candidate and a single `item_not_found` question are carried on the REJECT so Plan 3 can offer "create instead" without re-running the LLM.

`PendingSession` below is a **Plan 3 placeholder** (no code yet); `Resolution` remains sketched, but `question` now names the real type:

```python
class Resolution(BaseModel):
    target_key: str | None
    item_key: str | None
    field_values: dict[str, Any]        # key → final typed value
    answered: list[str]                 # question ids already answered

class PendingSession(BaseModel):
    chat_id: int
    original_text: str
    interpretation: Interpretation
    resolution: Resolution
    question: Question                  # validation.policy.Question (see above)
    created_at: datetime
    expires_at: datetime
```

## 5. Commands (`commands/models.py`, `commands/executor.py`)

```python
class PropertyWrite(BaseModel):         # extra="forbid"
    property_id: str
    property_name: str
    type: str
    value: Any = None                   # None = clear the property

class CreateItem(BaseModel):
    action: Literal["create_item"] = "create_item"
    data_source_id: str; target_name: str; properties: list[PropertyWrite]
class UpdateItem(BaseModel):
    action: Literal["update_item"] = "update_item"
    page_id: str; target_name: str; item_title: str; properties: list[PropertyWrite]
class CreatePage(BaseModel):
    action: Literal["create_page"] = "create_page"
    parent_page_id: str; target_name: str; title: str; body: list[str] = []
class AppendBlocks(BaseModel):
    action: Literal["append_blocks"] = "append_blocks"
    page_id: str; target_name: str; page_title: str; paragraphs: list[str]
class Search(BaseModel):
    action: Literal["search"] = "search"
    data_source_id: str | None; target_name: str; title_property: str | None; query: str

Command = CreateItem | UpdateItem | CreatePage | AppendBlocks | Search
RISK = {"create_item": "LOW", "create_page": "LOW", "append_blocks": "LOW",
        "search": "LOW", "update_item": "MEDIUM"}   # matches policy.RISK_BY_INTENT by intent
```

`PropertyWrite.value` JSON shapes (built by `commands/jsonvalue.py:to_json_value`, reused by `validation/policy.py` for `Question.proposed` and consumed by `notion/mapper.py:property_payload`); `value=None` always means "clear this property" (dropped entirely for `status`, since Notion cannot clear a status — see §8 of ARCHITECTURE.md):

| `type` | `value` shape |
|---|---|
| `title`, `rich_text` | `str` (mapper splits into 2000-char rich-text runs) |
| `select`, `status` | `{"id": str, "name": str}` or `None` |
| `multi_select`, `relation` | `list[{"id": str, "name": str}]` |
| `date` | `{"start": ISO str, "end": ISO str \| None}` or `None` |
| `checkbox` | `bool` |
| `number` | `int \| float` |
| `url` | `str` |

`Search` results are capped at `executor.SEARCH_LIMIT` (20) hits regardless of source (data-source query or global search).

`UndoRecord` (`commands/executor.py`, pydantic):

```python
class UndoRecord(BaseModel):
    kind: Literal["archive", "restore", "delete_blocks"]
    page_id: str | None = None
    properties: dict | None = None      # restore: property_id -> the shape property_payload RETURNS
                                         # (what update_page's properties dict expects), not the
                                         # PropertyWrite.value shape above
    block_ids: list[str] = []           # delete_blocks
    partial: bool = False               # restore: True when some updated properties could not be
                                         # captured before the write (record is still usable)
```

Undo by command: `CreateItem`/`CreatePage` → `archive` (`page_id`); `UpdateItem` → `restore` (`page_id` + `properties` captured from the page *before* the write, via `mapper.read_to_write`); `AppendBlocks` → `delete_blocks` (`block_ids` of the blocks just created); `Search` produces no `UndoRecord`.

## 6. SQLite (`data/bot.sqlite`)

```sql
CREATE TABLE events (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,                     -- ISO UTC
  telegram_user_id INTEGER NOT NULL,
  chat_id INTEGER NOT NULL,
  message_id INTEGER,
  kind TEXT NOT NULL,                   -- text | voice | callback
  raw_input TEXT,                       -- text or 'voice:<file_id>'
  transcription TEXT,
  llm_model TEXT,
  llm_context TEXT,                     -- JSON sent
  llm_response TEXT,                    -- raw JSON received
  interpretation TEXT,                  -- validated Interpretation JSON
  candidate_scores TEXT,                -- JSON [{target,confidence}]
  validation_result TEXT,               -- JSON {issues:[...]}
  decision TEXT,                        -- EXECUTE | CLARIFY | REJECT
  clarification_state TEXT,             -- JSON question or null
  command TEXT,                         -- JSON command or null
  executed INTEGER NOT NULL DEFAULT 0,
  notion_page_id TEXT,
  error TEXT,
  duration_ms INTEGER
);

CREATE TABLE sessions (
  chat_id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL,                -- PendingSession JSON
  expires_at TEXT NOT NULL
);

CREATE TABLE executions (
  id INTEGER PRIMARY KEY,
  event_id INTEGER REFERENCES events(id),
  chat_id INTEGER NOT NULL,
  reply_message_id INTEGER,
  undo TEXT NOT NULL,                   -- JSON: {kind: archive|restore|delete_blocks, ...}
  undone INTEGER NOT NULL DEFAULT 0,
  expires_at TEXT NOT NULL
);
```

Secrets are never written. `llm_context` contains keys, not Notion ids.

## 7. Settings (`.env`)

| Key | Default | Notes |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | |
| `TELEGRAM_ALLOWED_USER_IDS` | required | comma-separated |
| `NOTION_TOKEN` | required | internal integration secret |
| `NOTION_VERSION` | `2025-09-03` | |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | point at remote host later |
| `LLM_MODEL` | `qwen3:8b` | |
| `LLM_TEMPERATURE` | `0` | |
| `LLM_NUM_CTX` | `16384` | |
| `LLM_TIMEOUT_S` | `120` | |
| `WHISPER_MODEL` | `large-v3-turbo` | |
| `WHISPER_DEVICE` | `auto` | `cuda` / `cpu` |
| `WHISPER_COMPUTE_TYPE` | `int8` | |
| `WHISPER_LANGUAGE` | `ru` | empty = autodetect |
| `TIMEZONE` | `Europe/Tallinn` | |
| `LOCALE` | `ru` | |
| `DB_PATH` | `data/bot.sqlite` | |
| `TARGETS_FILE` | `data/targets.yaml` | |
| `SCHEMA_CACHE_TTL_S` | `60` | |
| `ITEMS_PER_TARGET` | `15` | |
| `ADMIN_UI_PORT` | `8787` | `0` disables |
| `POLICY_INTENT_MIN` | `0.85` | |
| `POLICY_TARGET_MIN` | `0.85` | |
| `POLICY_TARGET_MARGIN` | `0.10` | |
| `POLICY_FIELD_MIN` | `0.75` | |
| `POLICY_DATE_MIN` | `0.80` | |
| `SESSION_TTL_S` | `900` | pending clarification lifetime |
| `UNDO_WINDOW_S` | `300` | |
| `LOG_LEVEL` | `INFO` | |
