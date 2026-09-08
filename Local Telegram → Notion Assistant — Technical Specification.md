# Local Telegram → Notion Assistant
## Technical Specification / Architecture Requirements

### 1. Purpose

Build a self-hosted personal Telegram assistant that accepts text and voice messages, interprets them using a local LLM, and safely creates, updates, searches, or appends information in a Notion workspace.

The system must prioritize:

- deterministic behavior;
- explicit control over what the LLM is allowed to do;
- local inference;
- strict validation;
- preservation of optional as well as required fields;
- explicit handling of ambiguity;
- auditable operations;
- modularity, so individual components can later be replaced.

The LLM must **not** have unrestricted access to Notion and must not directly decide which Notion API operations to execute.

---

# 2. Core Architecture

```text
Telegram
    │
    ├── Text
    │
    └── Voice
          │
          ↓
   Speech-to-Text
   faster-whisper
          │
          ↓
   Normalized User Input
          │
          ↓
   ┌──────────────────────┐
   │       Local LLM      │
   │                      │
   │ Classification       │
   │ Field extraction     │
   │ Confidence estimates │
   │ Ambiguity detection  │
   └──────────┬───────────┘
              │
              ↓
   Structured Interpretation
              │
              ↓
   ┌──────────────────────┐
   │ Deterministic        │
   │ Validator / Policy   │
   │ Engine               │
   └──────────┬───────────┘
              │
       ┌──────┼───────┐
       ↓      ↓       ↓
    Execute Clarify Reject
       │      │
       │      ↓
       │   Telegram
       │   clarification
       │      │
       │      ↓
       │   Resolve
       │
       ↓
   NotionService
       │
       ↓
   Notion API
       │
       ↓
   Telegram response
```

The application, not the LLM, owns the final decision.

---

# 3. Fundamental Design Principle

The LLM is an **interpretation engine**, not an autonomous Notion agent.

It performs:

1. intent classification;
2. target classification;
3. extraction of fields/entities;
4. confidence estimation;
5. detection of missing or ambiguous information.

It does **not**:

- call Notion;
- construct arbitrary Notion API requests;
- choose arbitrary API endpoints;
- invent database/table/property names;
- invent property values;
- execute multiple actions autonomously;
- bypass validation;
- decide whether a destructive operation is permitted.

The application decides whether the interpretation is sufficiently reliable to execute.

---

# 4. Full Target Schema Must Be Provided to the LLM

A critical requirement is that the LLM must receive the **complete set of writable target schemas in a single interpretation context**.

The system must not perform:

```text
Step 1: classify target
Step 2: retrieve fields for target
Step 3: ask another LLM call to parse fields
```

Instead, the LLM receives all available target options, including:

- target/table identity;
- description;
- all required fields;
- all optional fields;
- field types;
- allowed values;
- descriptions of fields;
- relationships where relevant;
- constraints relevant to interpretation.

Example:

```json
{
  "targets": [
    {
      "id": "todo",
      "name": "Todo",
      "description": "Tasks that need to be completed",
      "fields": {
        "title": {
          "type": "string",
          "required": true,
          "description": "Task title"
        },
        "priority": {
          "type": "select",
          "required": true,
          "options": ["A", "B", "C"]
        },
        "due": {
          "type": "date",
          "required": false
        },
        "project": {
          "type": "relation",
          "required": false,
          "options": ["Home", "Work", "House"]
        }
      }
    },
    {
      "id": "buy",
      "name": "Buy",
      "description": "Things that need to be purchased",
      "fields": {
        "title": {
          "type": "string",
          "required": true
        },
        "shop": {
          "type": "string",
          "required": false
        },
        "category": {
          "type": "select",
          "required": false,
          "options": ["Food", "Household", "Tools"]
        }
      }
    }
  ]
}
```

The LLM must classify the target **and parse fields against the selected target schema in the same inference**.

---

# 5. Target and Field Parsing Are Coupled

Target selection directly affects the interpretation of the input.

For example:

> "Купить хлеб завтра"

Possible interpretations:

```text
Buy
    title = "Хлеб"
    shop = null

Todo
    title = "Купить хлеб"
    due = tomorrow
```

These are not merely two different targets with the same extracted fields.

The target changes the semantic interpretation of the sentence and therefore changes the field extraction.

The LLM must therefore return a complete candidate interpretation for each meaningful target rather than performing independent classification and extraction.

Conceptually:

```text
User input
    ↓
┌─────────────────────────────────────┐
│ Candidate interpretation A          │
│ target = buy                        │
│ fields = {title: "Хлеб"}            │
│ confidence = ...                    │
├─────────────────────────────────────┤
│ Candidate interpretation B          │
│ target = todo                       │
│ fields = {                          │
│   title: "Купить хлеб",             │
│   due: 2026-09-09                   │
│ }                                   │
│ confidence = ...                    │
└─────────────────────────────────────┘
```

The validator then decides whether the best candidate is sufficiently better than the alternatives.

---

# 6. Optional Fields Must Never Be Lost

Optional fields are first-class information.

The model must distinguish between:

```text
field not mentioned
```

and:

```text
field mentioned but value could not be determined
```

and:

```text
field explicitly set to null/empty
```

These states must not be collapsed.

For example:

```json
{
  "shop": {
    "status": "not_mentioned"
  }
}
```

is different from:

```json
{
  "shop": {
    "status": "ambiguous",
    "candidates": ["Rimi", "Prisma"]
  }
}
```

and different from:

```json
{
  "shop": {
    "status": "value",
    "value": "Rimi"
  }
}
```

The application must preserve optional fields even when they are not populated.

The LLM must not silently discard information simply because a field is optional.

---

# 7. Structured LLM Output

The LLM must return a strict application-level schema validated using Pydantic.

A conceptual response:

```json
{
  "intent": {
    "value": "create",
    "confidence": 0.98
  },

  "candidates": [
    {
      "target": "buy",
      "confidence": 0.91,
      "fields": {
        "title": {
          "status": "value",
          "value": "Хлеб",
          "confidence": 0.99
        },
        "shop": {
          "status": "not_mentioned"
        },
        "category": {
          "status": "not_mentioned"
        }
      }
    },
    {
      "target": "todo",
      "confidence": 0.84,
      "fields": {
        "title": {
          "status": "value",
          "value": "Купить хлеб",
          "confidence": 0.98
        },
        "priority": {
          "status": "not_mentioned"
        },
        "due": {
          "status": "value",
          "value": "2026-09-09",
          "confidence": 0.94
        }
      }
    }
  ],

  "ambiguities": [],
  "missing_required_fields": []
}
```

The exact schema should be implemented with Pydantic discriminated unions where appropriate.

---

# 8. Confidence Is an Input to the Policy Engine

The LLM may provide confidence scores, but these scores are **not treated as mathematically calibrated probabilities by default**.

They are confidence estimates.

The application uses them according to explicit policies.

Example:

```text
Best candidate:       0.91
Second candidate:     0.84
Difference:           0.07
```

If the configured policy requires:

```text
best >= 0.90
AND
(best - second) >= 0.10
```

the application must ask for clarification.

Another example:

```text
Best candidate:       0.96
Second candidate:     0.71
Difference:           0.25
```

This may be accepted automatically.

Thresholds must be configurable rather than hard-coded.

---

# 9. Confidence Must Be Multi-Dimensional

At minimum, distinguish:

```text
intent_confidence
target_confidence
field_confidence
```

For date/time entities, confidence should be particularly explicit because temporal language can be ambiguous.

Example:

```json
{
  "due": {
    "status": "value",
    "value": "2026-09-14",
    "confidence": 0.63,
    "source_text": "в следующий понедельник"
  }
}
```

The policy engine may decide that 0.63 is insufficient and request confirmation.

---

# 10. Deterministic Decision Policy

The application must contain a deterministic decision layer.

Conceptually:

```python
decision = policy.evaluate(interpretation)
```

Possible results:

```text
EXECUTE
CLARIFY
REJECT
```

Example policy:

```text
EXECUTE when:
    intent confidence >= threshold
    target confidence >= threshold
    target margin >= threshold
    all required fields are valid
    all enum/select values are valid
    all semantic constraints pass
    authorization passes

CLARIFY when:
    target is ambiguous
    required field is missing
    date/time is ambiguous
    important field confidence is too low
    semantic interpretation has multiple plausible candidates

REJECT when:
    requested operation is unsupported
    target is not writable
    field does not exist
    value violates schema
    authorization fails
```

---

# 11. Clarification Is a First-Class State

Clarification must not be treated as an error.

The assistant should maintain an explicit conversation state.

Example:

```text
NEW
 ↓
INTERPRETED
 ↓
VALIDATING
 ↓
 ├── EXECUTE
 │
 ├── CLARIFY
 │      ↓
 │   WAITING_FOR_USER
 │      ↓
 │   RESOLVE
 │      ↓
 │   VALIDATE
 │
 └── REJECT
```

Example:

User:

> "Купить хлеб"

System:

```text
Buy: 91%
Todo: 84%
```

Policy determines that the margin is insufficient.

Telegram asks:

> Куда добавить «Хлеб»?

```text
[Покупки] [Задачи]
```

User selects:

> Покупки

The application updates the pending interpretation:

```text
target = buy
```

and re-runs deterministic validation.

The LLM does not need to reconsider the already resolved decision unless additional natural-language input introduces new information.

---

# 12. Clarification Can Also Supply Missing Fields

Example:

> "Добавь задачу подготовить документы"

The selected target requires:

```text
title
priority
```

If priority is required but absent:

> Какой приоритет?

```text
[A] [B] [C]
```

User:

> A

The application adds:

```json
{
  "priority": "A"
}
```

and continues validation.

---

# 13. Clarification May Resolve Ambiguous Values

Example:

> "Купить продукты в Рими"

If multiple allowed shop values exist:

```text
Rimi
Rimi Hyper
Rimi Express
```

and the model cannot determine the exact value:

```text
shop:
    status = ambiguous
    candidates = [...]
```

The assistant asks for clarification instead of guessing.

---

# 14. Schema Registry

The application must maintain a normalized internal representation of the writable Notion schema.

The registry should contain:

```text
Target
 ├── id
 ├── display name
 ├── description
 ├── writable?
 ├── operations supported
 └── fields
       ├── id
       ├── name
       ├── type
       ├── required
       ├── description
       ├── allowed values
       ├── relation targets
       └── constraints
```

The registry is generated/refreshed from Notion and may additionally contain application-level semantic metadata.

Example:

```yaml
targets:

  - id: todo
    name: Todo
    description: Tasks that need to be completed

    fields:
      title:
        type: string
        required: true

      priority:
        type: select
        required: true
        options:
          - A
          - B
          - C

      due:
        type: date
        required: false

      project:
        type: relation
        required: false

  - id: buy
    name: Buy
    description: Things that need to be purchased

    fields:
      title:
        type: string
        required: true

      shop:
        type: string
        required: false

      category:
        type: select
        required: false
        options:
          - Food
          - Household
          - Tools
```

---

# 15. Nested Targets and Complete Structures

The schema supplied to the LLM may contain nested structures.

For example:

```json
{
  "targets": [
    {
      "id": "home",
      "name": "Home",
      "children": [
        {
          "id": "todo",
          "name": "Todo",
          "fields": {}
        },
        {
          "id": "buy",
          "name": "Buy",
          "fields": {}
        }
      ]
    }
  ]
}
```

The LLM must receive the complete relevant hierarchy whenever hierarchy affects classification.

The application must not assume that all targets exist at a single flat level.

---

# 16. Candidate Generation

The model should be encouraged to return the best candidate plus meaningful alternatives.

For example:

```json
{
  "candidates": [
    {
      "target": "buy",
      "confidence": 0.91,
      "fields": { ... }
    },
    {
      "target": "todo",
      "confidence": 0.84,
      "fields": { ... }
    }
  ]
}
```

The model should not be required to return every possible target when that would create unnecessary output.

It should return:

- the best interpretation;
- sufficiently plausible alternatives;
- complete field extraction for each returned candidate.

---

# 17. Semantic Validation

After Pydantic validation, the application must perform semantic validation.

Examples:

```text
Does target exist?
Is target writable?
Does field exist?
Is field valid for this target?
Is field required?
Is the value valid for its type?
Is a select value one of the allowed values?
Is a relation target valid?
Is the date valid?
Is the operation supported?
```

The validator must operate against the current schema registry.

---

# 18. Authorization and Safety

The LLM must never receive secrets.

The Notion token remains exclusively inside the application.

The LLM must not have access to:

```text
NOTION_TOKEN
Telegram bot token
HTTP clients
filesystem execution
shell execution
arbitrary URLs
Python execution
```

Only explicit application commands can reach the Notion layer.

No arbitrary tool execution should be exposed to the model.

---

# 19. Application-Level Command Model

LLM output must represent application-level intent, not Notion API payloads.

Examples:

```text
CreateDatabaseItem
UpdateDatabaseItem
CreatePage
UpdatePage
AppendBlocks
Search
```

These commands must contain only fields relevant to the application's domain.

For example:

```python
class CreateTask(BaseModel):
    action: Literal["create_task"]
    target: Literal["todo"]
    fields: TodoFields
```

The application maps this to the appropriate Notion API request.

---

# 20. Notion Provider Abstraction

The system should use a provider abstraction:

```text
NotionProvider
    │
    ├── DirectNotionProvider
    │
    └── NotionMCPProvider
```

The initial implementation should use the direct Notion API.

MCP may be implemented later as an alternative provider.

The rest of the application must not depend on whether Notion is accessed through REST API or MCP.

This keeps the architecture extensible while preserving deterministic control.

---

# 21. Direct Notion API

Initial implementation should use direct HTTP access to the Notion API through `httpx.AsyncClient`, or the official Notion SDK where it provides sufficient control.

The Notion layer is responsible for translating application commands into Notion API payloads.

The LLM never constructs those payloads.

Relevant operations include:

```text
create page/database item
update page
query data source
retrieve page
retrieve block children
append blocks
search
retrieve schema
```

---

# 22. Voice Pipeline

Voice messages:

```text
Telegram voice
    ↓
download audio
    ↓
faster-whisper
    ↓
transcription
    ↓
normalized text
    ↓
LLM
```

Initial speech-to-text candidate:

```text
faster-whisper large-v3
```

with GPU acceleration where available.

A smaller model such as `distil-large-v3` may be used if latency/resource requirements justify it.

The transcription itself should be retained in the audit log.

---

# 23. Local LLM Runtime

Use Ollama as the local inference runtime.

The LLM model must be configurable.

Candidate models should be benchmarked on the actual hardware before final selection.

The LLM must support:

- structured JSON output;
- JSON Schema;
- sufficiently strong instruction following;
- Russian language;
- reliable classification;
- reliable extraction;
- low-temperature deterministic operation.

Initial generation should use low/zero temperature where supported.

The model must be accessed through a dedicated `LLMClient` abstraction.

```python
class LLMClient:

    async def interpret(
        self,
        text: str,
        schema_context: dict,
        conversation_context: dict | None = None,
    ) -> Interpretation:
        ...
```

---

# 24. Context Construction

The prompt/context builder is responsible for constructing the complete interpretation context.

It must include:

```text
system instructions
+
current user input
+
current date/time/timezone
+
complete relevant target schema
+
allowed operations
+
application-specific semantic descriptions
+
conversation state when resolving clarification
```

The model must not be expected to infer the current date from training data.

---

# 25. Temporal Interpretation

Date/time expressions must be resolved relative to an explicit application time context.

Examples:

```text
today
tomorrow
Friday
next Monday
in two weeks
послезавтра
в пятницу
на следующей неделе
```

The context should explicitly provide:

```text
current_datetime
timezone
locale
```

The LLM returns a normalized representation.

The application validates it.

Low-confidence or ambiguous temporal interpretations must trigger clarification.

---

# 26. Confirmation Policy

Operations have different risk levels.

Example:

```text
LOW
    create task
    create shopping item
    create note
    append content

MEDIUM
    update item
    change status
    move item

HIGH
    delete item
    bulk update
    bulk delete
```

Policy may automatically execute LOW-risk operations when interpretation confidence is sufficient.

MEDIUM-risk operations may require confirmation.

HIGH-risk operations always require explicit confirmation.

Telegram inline buttons can be used:

```text
[Confirm] [Edit] [Cancel]
```

---

# 27. Audit Log

Use SQLite.

At minimum store:

```text
id
timestamp
telegram_user_id
message_id
raw_input
transcription
llm_model
llm_response
interpreted_command
candidate_scores
validation_result
clarification_state
executed
notion_page_id
error
```

Secrets must never be logged.

The audit log should make it possible to reconstruct why the system executed or rejected an operation.

---

# 28. Testing Strategy

Testing must focus heavily on interpretation and decision boundaries.

Tests should include:

### Classification

```text
"купи хлеб"
"добавь хлеб в покупки"
"не забудь купить молоко"
"создай задачу купить молоко"
```

### Ambiguity

```text
"добавь хлеб"
```

when both Buy and Todo are plausible.

### Optional fields

Verify that optional fields are preserved and not accidentally dropped.

### Missing required fields

Verify that the system asks for missing information.

### Invalid values

```text
priority = "urgent"
```

when only A/B/C are allowed.

### Temporal ambiguity

```text
"сделай к понедельнику"
"на следующей неделе"
```

### Confidence thresholds

Test cases around:

```text
0.89 / 0.90
margin 0.09 / 0.10
```

### Security

Ensure that model output cannot cause arbitrary API calls.

---

# 29. Suggested Project Structure

```text
notion_bot/
│
├── app/
│   ├── telegram/
│   │   ├── handlers.py
│   │   ├── keyboards.py
│   │   └── state.py
│   │
│   ├── speech/
│   │   └── whisper.py
│   │
│   ├── llm/
│   │   ├── client.py
│   │   ├── prompts.py
│   │   ├── context.py
│   │   └── schemas.py
│   │
│   ├── interpretation/
│   │   ├── models.py
│   │   ├── classifier.py
│   │   ├── confidence.py
│   │   └── resolver.py
│   │
│   ├── validation/
│   │   ├── schema.py
│   │   ├── semantic.py
│   │   └── policy.py
│   │
│   ├── notion/
│   │   ├── client.py
│   │   ├── provider.py
│   │   ├── schema_registry.py
│   │   └── mapper.py
│   │
│   ├── commands/
│   │   ├── models.py
│   │   └── executor.py
│   │
│   └── database/
│       └── sqlite.py
│
├── tests/
│
├── config.py
├── main.py
├── pyproject.toml
└── .env
```

---

# 30. Initial Technology Stack

```text
Python              3.12+
Async runtime       asyncio
Telegram            python-telegram-bot
Validation          Pydantic 2.x
HTTP                 httpx
Database             SQLite
Speech               faster-whisper
LLM runtime          Ollama
Notion               Notion API
Testing              pytest
Configuration        python-dotenv
Logging              Python logging / structured logging
```

Exact package versions must be pinned after compatibility testing.

---

# 31. MVP Scope

### Include

- Telegram text input
- Telegram voice input
- local Whisper transcription
- local LLM through Ollama
- structured LLM output
- complete target/schema context
- target classification
- coupled field extraction
- confidence scores
- candidate alternatives
- deterministic confidence policy
- semantic validation
- clarification state
- interactive Telegram clarification
- creation of database items
- page creation
- page/item updates
- append content
- basic search
- schema discovery
- SQLite audit log
- authorization
- configurable models
- tests

### Do not include initially

- unrestricted autonomous agents
- direct LLM → Notion MCP execution
- arbitrary tool calling
- arbitrary HTTP requests
- arbitrary code execution
- destructive bulk operations
- full Notion Whiteboard/canvas manipulation
- multi-agent architecture
- cloud LLM fallback
- complex RAG system unless later justified by actual requirements

---

# 32. Important Architectural Constraint

The following architecture is explicitly **not allowed**:

```text
LLM
  ↓
MCP
  ↓
Notion
```

where the LLM independently decides which Notion tools to call.

The preferred architecture is:

```text
LLM
  ↓
Structured Interpretation
  ↓
Deterministic Validator / Policy Engine
  ↓
Application Command
  ↓
NotionProvider
  ↓
Notion
```

The LLM may suggest an interpretation.

Only the application may authorize and execute it.

---

# 33. Desired End State

The system should behave less like an autonomous agent and more like a **natural-language compiler**:

```text
Natural language
       ↓
LLM
       ↓
Structured intermediate representation
       ↓
Validation
       ↓
Deterministic policy
       ↓
Application command
       ↓
Notion operation
```

The LLM provides probabilistic interpretation.

The application provides deterministic execution.

This separation is a fundamental design requirement.