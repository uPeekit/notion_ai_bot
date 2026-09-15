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
    url: str                            # Notion page/data-source url
    is_inbox: bool = False              # set by Discovery._resolve_inbox from targets.yaml `inbox`
                                         # or the INBOX_TARGET_ID override; at most one target true

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
  inbox: true               # this is the fallback target for anything the pipeline can't resolve
```

Discovery adds missing ids with empty descriptions and never deletes entries. `inbox` (default
`false`, at most one `true` — `Discovery._resolve_inbox` demotes every entry but the lowest id if
more than one is flagged, logging a WARNING) is never touched by discovery itself, so it survives
every rediscovery; edited by hand today, from the admin page in Plan 3b. `INBOX_TARGET_ID` in
`.env` (§7) overrides this flag outright when set.

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
QType = Literal["target", "intent_confirm", "item", "item_not_found", "field_required",
                "field_ambiguous", "field_confirm", "date", "content_required",
                "nothing_to_write"]

class QOption(BaseModel):               # extra="forbid"; JSON-safe, session-persistable
    key: str                            # format depends on question type, see table below
    label: str

class Question(BaseModel):              # extra="forbid"; JSON-safe, session-persistable
    type: QType
    target_key: str | None = None       # candidate key the question concerns, e.g. "t3"
    field_key: str | None = None        # set for field_required / field_ambiguous / field_confirm / date
    field_name: str | None = None
    options: list[QOption] = []         # empty for field_confirm, date, item_not_found, content_required, intent_confirm, nothing_to_write
    proposed: Any = None                # JSON-safe: date -> {start, end, granularity}; option value -> {id, name} (jsonvalue.to_json_value); intent_confirm -> the intent string; item_not_found -> Candidate.item_text

    @property
    def id(self) -> str:                # f"{type}:{field_key or target_key or ''}" — stable within one context generation; embeds positional context keys, so it does NOT survive a rediscovery (see table below)
        ...

@dataclass
class Decision:
    kind: Literal["EXECUTE", "CLARIFY", "REJECT"]
    candidate: VCandidate | None         # set for EXECUTE, CLARIFY, and the item_not_found/nothing_to_write REJECTs; else None
    questions: list[Question]            # non-empty for CLARIFY; exactly one Question for the item_not_found/nothing_to_write REJECT cases; else []
    reasons: list[str]                   # REJECT: issue codes/messages, ["no valid candidates"], ["item not found in target"], or ["nothing to write"]; CLARIFY: question types in ask order
    risk: Literal["LOW", "MEDIUM"] | None  # RISK_BY_INTENT[intent]; None only when REJECT has no candidate
```

`QOption.key` format by question type (built in `policy.py`):

| Question type | Key format | Example |
|---|---|---|
| `target` | candidate key (the target key itself) | `t3` |
| `item` | `<target_key>.item:<page id>` | `t3.item:2f1c…a9` |
| `field_required`, when field has non-empty `options` | `<field_key>.o<1-based index>` | `t3.f2.o1` |
| `field_ambiguous` | `<field_key>#<0-based index>` | `t3.f2#0` |
| `field_confirm`, `date`, `item_not_found`, `content_required`, `intent_confirm`, `nothing_to_write` | no options; answer is free text or a confirm/other action | — |

Question order (ties within one `Decision.questions` broken by this order, one asked at a time): `target → intent_confirm → item → item_not_found → field_required → field_ambiguous → date → field_confirm → content_required → nothing_to_write`.

Special cases, both REJECT with the candidate and a single question carried (so the conversation layer can act without re-running the LLM):
- `update` intent with no resolvable item and no `item_candidates`: `Decision("REJECT", best, [Question("item_not_found", ..., proposed=best.item_text)], ["item not found in target"], risk)`.
- `update` intent with a resolved item but no field `status` in `("value", "explicit_null", "ambiguous")`: `Decision("REJECT", best, [Question("nothing_to_write", ...)], ["nothing to write"], risk)`. (An `ambiguous` field still needs a `field_ambiguous` CLARIFY round-trip — it is not "nothing to write", since resolving it produces a real value.)

`intent_confirm` replaces a one-option `target` question when the *only* reason to ask is `intent.confidence < POLICY_INTENT_MIN` and there is exactly one candidate (`proposed` carries the guessed intent string).

### Answer keys across a context rebuild

`Question.id` is stable only within one context generation, not across a rebuild (it embeds positional context keys). The per-question-type answer is likewise keyed into the *old* context and cannot be replayed after discovery re-runs; `PendingSession` persists the value below instead, then `resolver.py` re-resolves it against the fresh `Context` (via `Context.field_key`/`option_key`/`item_key`, §1 above):

| Question type | What must be persisted instead of the context key |
|---|---|
| `target` | `Target.id` |
| `item` | the item's page id |
| `field_required` | `(field_id, option_id)` for option types, else the typed value |
| `field_ambiguous` | the typed value itself (the one the user picked, not its context key) |
| `date`, `field_confirm` | the JSON `proposed` value (already Notion-id-based, not key-based) |

`PendingSession` (`conversation/session.py`) is real code — the payload of the `sessions` row
(§6). It follows the rule above to the letter: every field is a Notion id or a JSON-safe typed
value, never a context key, so it survives a rediscovery that renumbers every key:

```python
class PendingField(BaseModel):          # extra="forbid"; one VField flattened by Notion id
    field_id: str; name: str; type: str; status: str
    value: Any = None                   # to_json_value(VField.value)
    candidates: list[Any] = []          # to_json_value(c) for c in VField.candidates
    confidence: float = 1.0
    source_text: str = ""

class PendingCandidate(BaseModel):      # extra="forbid"; one VCandidate flattened by Notion id
    target_id: str
    confidence: float
    item_page_id: str | None            # VCandidate.item.id, not the item's context key
    item_text: str | None
    content: str | None
    search_query: str | None
    fields: list[PendingField]
    item_candidates: list[str] = []     # VCandidate.item_candidates as page ids, not "t2.i4":
                                         # Policy offers the `item` question only while this is
                                         # non-empty, so a session that dropped them turns the
                                         # second question of a round-trip into an
                                         # item_not_found REJECT offering to create a duplicate

class AnswerOption(BaseModel):          # extra="forbid"; one button on the question on screen
    id: str                             # o0..o7, or a literal "confirm"/"other"/"add_new"/
                                         # "cancel"/"inbox" — never a QOption.key (would embed
                                         # Notion page ids and blow Telegram's 64-byte payload)
    label: str
    target_id: str | None = None
    item_page_id: str | None = None
    field_id: str | None = None
    option_id: str | None = None
    value: Any = None

class PendingSession(BaseModel):        # extra="forbid"
    chat_id: int
    event_id: int
    token: str                          # secrets.token_hex(4); embedded in every "a:<token>:…"
                                         # callback payload; a stale/mismatched token → SESSION_EXPIRED
    original_text: str
    intent: str
    intent_confidence: float
    candidates: list[PendingCandidate]  # every validated candidate, so an alternate target/item
                                         # can be picked without re-running the LLM
    question: Question                  # validation.policy.Question — the one on screen now;
                                         # its target_key/field_key/options[].key ARE context
                                         # keys, kept on purpose so Task 3's resolver can re-
                                         # resolve them against a freshly built Context
    options: list[AnswerOption]         # the answer table for `question`
    asked: list[str] = []               # answer keys already spent against MAX_QUESTIONS (3)
    created_at: datetime
    expires_at: datetime
```

`AnswerOption` fields set per question type (`resolver.options_for`; `id`/`label` always set;
`cancel`/`inbox` are appended to every question's option list):

| Question type | Fields set besides `id`/`label` |
|---|---|
| `target` | `target_id` |
| `item` | `item_page_id` |
| `item_not_found` | `field_id` (the title field); single option, `id="add_new"` |
| `field_required` | `field_id`, `option_id` (select/status/relation options) |
| `field_ambiguous` | `field_id`, `value` (the typed candidate value itself) |
| `intent_confirm` | single option, `id="confirm"` |
| `date`, `field_confirm` | `field_id`; two options, `id="confirm"`/`id="other"` |
| every type | `cancel` (`id="cancel"`) and `inbox` (`id="inbox"`), unconditionally — whether the `[В разное]` *button* is actually shown is decided separately, by `reply.format_question`'s own `inbox` flag (an inbox target must be available) |

`apply_answer(session, option_id)` is pure and id-based — no `Context`, no `WorkspaceSnapshot`, no
I/O — and returns the updated session plus a control verb (`None` = re-evaluate with `Policy`,
`"cancel"`, `"inbox"`, or `"free_text"` for the `[Другое]` button on `date`/`field_confirm`).
`rebuild_candidate`/`result_from_session` turn a stored session back into a `ValidationResult`
against a *fresh* snapshot on every answer: a renamed target/field/option still resolves by id
(via `Context.target_key`/`field_key`/`option_key`); anything actually gone is dropped (with a
`SEM_UNKNOWN_KEY` issue for a vanished option) rather than raising.

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
    filters: list[PropertyWrite] = []   # from validated fields with status=value; see below

Command = CreateItem | UpdateItem | CreatePage | AppendBlocks | Search
# Risk is keyed by intent, not by command: see validation/policy.py:RISK_BY_INTENT.
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

`Search.filters` (`commands/builder.py:_search_filters`): one `PropertyWrite` per validated field
with status `value` whose type is `select`, `status`, `multi_select`, `relation` or `checkbox`
(same `PropertyWrite` shape as above). `notion/mapper.py:search_filter` turns these into a Notion
data-source query filter, addressed by property **name**: `select`/`status` → `{"property": name,
"<type>": {"equals": option_name}}`; `multi_select`/`relation` → one `contains` condition per
selected value (`relation` by page id, `multi_select` by option name); `checkbox` → `{"property":
name, "checkbox": {"equals": bool}}`; plus, when `query` is non-empty and `title_property` is set,
a `title` `contains` condition. Zero conditions → `None` (unfiltered); one → that condition; more
→ `{"and": [...]}`. A malformed filter value is skipped rather than raised (same defensive stance
as `property_payload`, §5 note above).

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
  executed INTEGER NOT NULL DEFAULT 0,   -- the command ran; a `search` sets it and writes nothing
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
  reply_message_id INTEGER,             -- null at insert (no reply yet); the transport fills it
                                        -- in via AuditStore.set_reply_message_id, to edit the
                                        -- Undo button away when the window closes
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
| `INBOX_MODE` | `auto` | `auto`\|`button`\|`off` — `auto` saves on every unresolvable path and still offers the button; `button` saves only on a button press; `off` disables the inbox entirely |
| `INBOX_TARGET_ID` | `` | Notion page/data-source id; overrides every `targets.yaml` `inbox: true` flag when set; logs a WARNING if it matches no discovered target |
