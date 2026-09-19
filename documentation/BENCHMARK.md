# LLM benchmark — 2026-09-10

Hardware: RTX 4070 Laptop (8 GB VRAM), Ryzen AI 9 HX 370. Ollama at `http://127.0.0.1:11434`.

## Run 1 (pre-tuning prompt, ITEMS_PER_TARGET=50)

Cases: `tests/fixtures/ru_cases.yaml` (44), context: `tools/sample_workspace.py`, `num_ctx=16384`, `temperature=0`.

| model | n | valid | intent | target | fields | all | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| llama3.1:8b | 44 | 100% | 89% | 95% | 73% | 57% | 9288 | 13202 |
| qwen3:8b | 44 | 100% | 84% | 91% | 77% | 55% | 11141 | 21625 |
| qwen2.5:7b-instruct | 44 | 100% | 86% | 95% | 61% | 45% | 7109 | 11171 |
| gemma3:4b | 44 | 100% | 91% | 84% | 59% | 45% | 7835 | 14952 |

### llama3.1:8b failures (19)
- `buy_shop_prisma`: Магазин: 'Rimi' != 'Prisma'
- `todo_remember`: intent update!=create
- `todo_project`: intent update!=create; Проект: [] != 'Работа'
- `amb_bread`: candidates 1<2
- `amb_tickets`: intent search!=create; candidates 1<2
- `missing_priority`: Приоритет.status value not in ['not_mentioned']
- `invalid_shop`: Магазин.status value not in ['not_mentioned', 'ambiguous']
- `date_tomorrow`: Срок: 'ambiguous' != '2026-09-10'
- `date_day_after`: Срок: 'ambiguous' != '2026-09-11'
- `date_friday`: Срок: 'ambiguous' != '2026-09-11'
- `date_next_monday`: Срок: 'ambiguous' != '2026-09-14'
- `date_in_week`: Срок: 'ambiguous' != '2026-09-16'
- `date_time`: Срок: 'ambiguous' != '2026-09-10T09:45'
- `update_bought`: item_candidates ['t2.i2'] item=t2.i2
- `update_status_done`: Статус: 'To do' != 'Done'
- `update_due`: Срок: 'ambiguous' != '2026-09-11'
- `update_not_found`: item_candidates ['t2.i2', 't2.i4'] item=t2.i2
- `append_books`: intent create!=append; item Отпуск 2027
- `unknown_weather`: intent search!=unknown

### qwen3:8b failures (20)
- `buy_shop_prisma`: Магазин: 'ambiguous' != 'Prisma'
- `buy_note`: Заметка.status not_mentioned not in ['value']
- `todo_project`: intent update!=create; Проект: 'not_mentioned' != 'Работа'
- `amb_bread`: candidates 1<2
- `amb_tickets`: candidates 1<2
- `missing_priority`: Приоритет.status explicit_null not in ['not_mentioned']
- `invalid_priority`: intent update!=create
- `invalid_shop`: Магазин.status explicit_null not in ['not_mentioned', 'ambiguous']
- `date_tomorrow`: intent update!=create
- `date_day_after`: Срок: {'start': '2026-09-12T00:00', 'end': '2026-09-12T23:59'} != '2026-09-11'
- `date_friday`: intent search!=create; target Покупки; Срок: None != '2026-09-11'
- `date_next_monday`: Срок: {'start': '2026-09-12T00:00+03:00', 'end': '2026-09-12T00:00+03:00'} != '2026-09-14'
- `date_in_week`: intent search!=create
- `date_next_week_vague`: intent update!=create
- `date_explicit`: target Покупки; Срок: None != '2026-10-15'
- `update_bought`: item_candidates ['t2.i1'] item=t2.i1
- `update_due`: Срок: {'start': '2026-09-10T00:00', 'end': '2026-09-10T23:59'} != '2026-09-11'
- `update_not_found`: item_candidates ['t2.i1'] item=t2.i1
- `update_shop`: intent create!=update
- `append_books`: item Отпуск 2027

### qwen2.5:7b-instruct failures (24)
- `buy_quantity`: Количество: 'ambiguous' != 6
- `buy_note`: Заметка.status not_mentioned not in ['value']
- `todo_remember`: intent append!=create
- `todo_project`: intent update!=create; Проект: 'not_mentioned' != 'Работа'
- `todo_tags`: Теги: ['дом'] != ['здоровье']
- `todo_link`: Ссылка: 'not_mentioned' != 'https://example.com/a'
- `amb_bread`: candidates 1<2
- `amb_tickets`: intent search!=create
- `missing_priority`: Приоритет.status ambiguous not in ['not_mentioned']
- `invalid_shop`: Магазин.status value not in ['not_mentioned', 'ambiguous']
- `optional_kept`: Количество: 'ambiguous' != 10
- `date_tomorrow`: Срок: 'not_mentioned' != '2026-09-10'
- `date_day_after`: Срок: 'not_mentioned' != '2026-09-11'
- `date_friday`: intent update!=create; Срок: 'not_mentioned' != '2026-09-11'
- `date_next_monday`: Срок: 'not_mentioned' != '2026-09-14'
- `date_in_week`: intent update!=create; Срок: 'not_mentioned' != '2026-09-16'
- `date_time`: Срок: 'not_mentioned' != '2026-09-10T09:45'
- `date_explicit`: intent update!=create; Срок: 'not_mentioned' != '2026-10-15'
- `update_bought`: item_candidates [] item=t2.i1
- `update_status_done`: Статус: 'To do' != 'Done'
- `update_due`: target Покупки; item Молоко овсяное; Срок: None != '2026-09-11'
- `update_not_found`: item_candidates [] item=t2.i1
- `append_trip`: item None
- `append_books`: item None

### gemma3:4b failures (24)
- `buy_with_category`: Категория: 'Еда' != 'Инструменты'
- `buy_shop_prisma`: Магазин: 'ambiguous' != 'Prisma'
- `todo_remember`: intent unknown!=create
- `todo_project`: intent update!=create; Проект: 'not_mentioned' != 'Работа'
- `todo_tags`: Теги: ['дом', 'работа', 'здоровье'] != ['здоровье']
- `todo_link`: intent search!=create; Ссылка: 'not_mentioned' != 'https://example.com/a'
- `amb_bread`: candidates 1<2
- `amb_tickets`: candidates 1<2
- `missing_priority`: Приоритет.status value not in ['not_mentioned']
- `invalid_shop`: Магазин.status value not in ['not_mentioned', 'ambiguous']
- `optional_kept`: Магазин: 'Rimi' != 'Maxima'
- `date_tomorrow`: Срок: 'ambiguous' != '2026-09-10'
- `date_day_after`: Срок: 'ambiguous' != '2026-09-11'
- `date_friday`: target Покупки; Срок: None != '2026-09-11'
- `date_next_monday`: target Покупки; Срок: None != '2026-09-14'
- `date_in_week`: Срок: 'ambiguous' != '2026-09-16'
- `date_time`: Срок: 'ambiguous' != '2026-09-10T09:45'
- `date_explicit`: target Покупки; Срок: None != '2026-10-15'
- `update_bought`: item_candidates ['t2.i2', 't2.i3', 't2.i4'] item=t2.i2
- `update_bought_exact`: Куплено: False != True
- `update_status_done`: Статус: 'not_mentioned' != 'Done'
- `update_due`: target Покупки; item Яйца; Срок: None != '2026-09-11'
- `update_not_found`: item_candidates ['t2.i1', 't2.i2', 't2.i3', 't2.i4'] item=t2.i4
- `append_books`: intent create!=append; target Задачи; item Купить билеты

### Three most common failure patterns (Run 1, across all four models)

1. **Dates.** By far the largest category — `date_tomorrow`, `date_day_after`, `date_friday`,
   `date_next_monday`, `date_in_week`, `date_time`, `date_explicit`, and `update_due` fail for
   nearly every model. Models mark relative dates `ambiguous` or `not_mentioned` instead of
   resolving them against `now`/`weekday`, or (qwen3) return an over-eager `{start, end}` range
   for a single-day request.
2. **Ambiguity / candidate handling.** `amb_bread`, `amb_tickets`, `update_bought`, and
   `update_not_found` fail across all four models: given genuinely ambiguous text or multiple
   matching items, models collapse to a single guess instead of populating `candidates` /
   `item_candidates` as the prompt instructs.
3. **Select/status field semantics.** `buy_shop_prisma`, `missing_priority`, `invalid_shop`,
   `todo_project`, and `todo_tags` fail across all four models: models confuse `value` /
   `ambiguous` / `explicit_null` / `not_mentioned` status codes for select-like fields, or pick a
   near-match option instead of leaving it `ambiguous`/`not_mentioned` as instructed.

This run used the pre-tuning prompt (no calendar block, no few-shot examples) and
`ITEMS_PER_TARGET=50`; the scorer's `safe`/`wrong` columns did not exist yet and its list-vs-scalar
matcher bug (see Run 2) was still present, so these numbers are not directly comparable to Run 2
column-for-column beyond `valid`/`intent`/`target`/`fields`/`all`/latency.

## Run 2 (tuned prompt, calendar, ITEMS_PER_TARGET=15)

Changes since Run 1: calendar block added to context + a prompt rule to use it instead of doing
date arithmetic (Step 2); two few-shot "Примеры" for target/item ambiguity (Step 3); the
benchmark's `safe`/`wrong` scoring fixed to account for `statuses` expectations and to track
`wrong_value` directly instead of deriving it from `fields_ok`/`safe_ok` (Step 1); the list/scalar
matcher bug in `_match` fixed (a scalar expectation like `Проект: Работа` now matches a
single-item actual list) (Step 4 prep). Only the two best models from Run 1 were re-run.

Cases: `tests/fixtures/ru_cases.yaml` (44), context: `tools/sample_workspace.py`, `num_ctx=16384`,
`items_per_target=15`, `temperature=0`.

| model | n | valid | intent | target | fields | all | safe | wrong | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| llama3.1:8b | 44 | 100% | 86% | 100% | 70% | 57% | 73% | 7 | 17304 | 30468 |
| qwen3:8b | 44 | 100% | 93% | 91% | 82% | 73% | 77% | 8 | 13516 | 26296 |

### llama3.1:8b failures (19)
- `buy_simple`: Название: 'not_mentioned' != 'Молоко'
- `buy_explicit_list`: Название: 'not_mentioned' != 'Хлеб'
- `buy_with_category`: Название: 'not_mentioned' != 'Молоток'
- `buy_shop_prisma`: Название: 'ambiguous' != 'Сыр'; Магазин: 'not_mentioned' != 'Prisma'
- `buy_note`: Название: 'not_mentioned' != '*'
- `todo_project`: intent update not in ['create']; Проект: ['Дом'] != 'Работа'
- `missing_priority`: Приоритет.status value not in ['not_mentioned']
- `invalid_shop`: Магазин.status value not in ['not_mentioned', 'ambiguous']
- `date_friday`: Срок: {'start': '2026-09-10', 'end': None} != '2026-09-11'
- `date_time`: Срок: {'start': '2026-09-10T09:00+03:00', 'end': None} != '2026-09-10T09:45'
- `date_next_week_vague`: Срок.confidence 1.0>0.85
- `update_bought_exact`: intent create not in ['update']; item None
- `update_status_done`: Статус: 'To do' != 'Done'
- `update_due`: item None; Срок: {'start': '2026-09-10', 'end': None} != '2026-09-11'
- `update_shop`: intent create not in ['update']; item None
- `append_ideas`: intent create not in ['append']
- `append_trip`: item None
- `append_books`: intent create not in ['append']; item None
- `unknown_weather`: intent search not in ['unknown']

### qwen3:8b failures (12)
- `buy_shop_prisma`: Магазин: 'Rimi' != 'Prisma'
- `todo_tags`: Теги: ['дом'] != ['здоровье']
- `invalid_shop`: Магазин.status value not in ['not_mentioned', 'ambiguous']
- `date_tomorrow`: intent update not in ['create']
- `date_friday`: target Покупки; Срок: None != '2026-09-11'
- `date_in_week`: intent update not in ['create']; target Покупки; Срок: None != '2026-09-16'
- `date_time`: target Покупки; Срок: None != '2026-09-10T09:45'
- `date_explicit`: target Покупки; Срок: None != '2026-10-15'
- `update_bought`: item_candidates [] item=t2.i2
- `update_due`: Срок: {'start': '2026-09-10', 'end': None} != '2026-09-11'
- `update_not_found`: item_candidates [] item=t2.i1
- `unknown_weather`: intent search not in ['unknown']

**Regression to flag:** after the Step 3 few-shot examples were added, llama3.1:8b started
returning `Название: not_mentioned` (once `ambiguous`) for five plain `Покупки` create cases that
have no target or title ambiguity at all — `buy_simple`, `buy_explicit_list`,
`buy_with_category`, `buy_shop_prisma`, `buy_note`. None of these failed in Run 1. `target` for
llama1 is otherwise a clean 100% (vs 95% in Run 1), so the few-shot section fixed target selection
but appears to have made the model over-cautious about filling the title field on the `Покупки`
target specifically — plausibly primed by the "добавь хлеб" ambiguity example, whose first
candidate is also a `Покупки`-shaped entry. qwen3:8b shows no equivalent regression. This is worth
a follow-up prompt tweak (e.g. a title-specific counter-example) rather than a blocker for this
benchmark, since it fully explains llama3.1's `fields`/`all` drop and is isolated to one field on
one target.

## Run 3 (2026-09-16) — context size and model capacity

Question: was the 8B class a measured choice or an unexamined ceiling? Measured on the same
machine, first in VRAM and then in accuracy.

**VRAM (measured with `ollama ps`, prompts as configured, Whisper not loaded):**

| config | resident | GPU/CPU split |
|---|---|---|
| `qwen3:8b` @ `num_ctx=16384` | 7.8 GB | 80% GPU / 20% CPU |
| `qwen3:8b` @ `num_ctx=8192` | 6.2 GB | **100% GPU** |
| `mistral-nemo:12b` (q4) @ 8192 | 8.6 GB | 73% GPU / 27% CPU |
| `mistral-nemo:12b` q3_K_M @ 8192 | 7.6 GB | 83% GPU / 17% CPU |

The card has 8188 MiB, so nothing in the 12B class fits: the offload *is* the latency cost.
`qwen3:8b` at 16k was already over the line — the shipped default was paying a 20% CPU offload
for a context window the prompts (2-4k tokens) never used.

**Accuracy (44 cases, `items_per_target=15`, temperature 0, all run 2026-09-16):**

| model | n | valid | intent | target | fields | all | safe | wrong | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|---|---|
| qwen3:8b @ 8192 | 44 | 100% | 86% | 91% | 84% | 68% | 75% | 6 | 7468 | 12467 |
| qwen3:8b @ 16384 | 44 | 100% | 89% | 91% | 82% | 70% | 77% | 7 | 11468 | 20812 |
| mistral-nemo:12b q3_K_M @ 8192 | 44 | 100% | 98% | 93% | 86% | 75% | 86% | 4 | 12593 | 21328 |
| **mistral-nemo:12b @ 8192** | 44 | 100% | 95% | 98% | 91% | **82%** | **91%** | **2** | 15905 | 26656 |

### mistral-nemo:12b failures (8)
- `buy_shop_prisma`: Магазин: 'not_mentioned' != 'Prisma'
- `todo_project`: Проект: 'not_mentioned' != 'Работа'
- `date_friday`: target Покупки; Срок: None != '2026-09-11'
- `date_time`: Срок: {'start': '2026-09-10T06:45:00+03:00', 'end': None} != '2026-09-10T09:45'
- `update_bought`: item_candidates [] item=t2.i2
- `update_not_found`: item_candidates [] item=t2.i1
- `update_shop`: intent create not in ['update']; item None
- `unknown_weather`: intent search not in ['unknown']

**On run-to-run variance.** `qwen3:8b` @ 16384 scored 73% `all` / 77% `safe` in Run 2 and 70% /
77% today at identical settings — one or two cases drift even at temperature 0. Differences of
one or two cases are therefore noise; the 8k-vs-16k accuracy gap is within it, while the 35%
latency difference is structural and reproducible.

**Note on the failure set.** `update_bought`/`update_not_found` (collapsing to one item instead of
returning `item_candidates`) and `unknown_weather` fail for *every* model tested, 4B through 12B.
Capacity does not fix them; they are prompt/schema bugs. Likewise `date_friday`/`date_explicit`
are target-selection failures wearing a date costume: the model picks Покупки, which has no date
field, so a correctly-parsed date is silently dropped. That one is a target-description problem,
and the descriptions in `tools/sample_workspace.py` are synthetic — it needs a live workspace to
tune honestly.

## Decision

**`mistral-nemo:12b` at `LLM_NUM_CTX=8192` is the default** (2026-09-16), replacing `qwen3:8b` at
16384. Decision rule unchanged: highest `safe`, `wrong` as a tie-break penalty, then `all`, then
p50 — and the 12B wins the primary metric outright, 91% vs 77%, with a third as many
confidently-wrong values (2 vs 7). It also leads `intent` (95%), `target` (98%) and `fields`
(91%).

The cost is latency: p50 15.9 s vs 11.5 s, because 27% of the model runs on CPU. That trade was
taken deliberately — for a fire-and-forget assistant, a wrong row written to Notion costs manual
cleanup, while five extra seconds costs nothing but waiting. `qwen3:8b` @ 8192 remains the
documented fast alternative (7.5 s p50, 100% GPU-resident) and is a one-line `.env` change.

`mistral-nemo:12b-instruct-2407-q3_K_M` was tested specifically to see whether a smaller
quantisation would fit fully in VRAM. It does not (7.6 GB, still 17% offloaded), and it gave back
half the accuracy gain (`safe` 86%, `wrong` 4) to save 3.3 s. Rejected.

Because the 12B leaves no VRAM spare, `.env.example` now ships `WHISPER_DEVICE=cpu`; on a larger
GPU, or with `LLM_MODEL=qwen3:8b`, `auto` is correct again.

`qwen2.5:7b-instruct` and `gemma3:4b` were not re-benchmarked in Run 2 or Run 3 (only the leading
candidates were re-run); they trailed badly in Run 1 (45% `all` each) and there is no reason to
expect the prompt/calendar changes close that gap, but this is inference, not measurement — a
future full re-run should confirm before treating them as ruled out.

## Run 4 (2026-09-19) — reasoning first, named targets

A live message, «Купи слона.», was sent to `Databases` (0.7) / `Books` (0.3) — the first two
targets in the list — while the model's own `notes` said "a shopping item or a task". With
opaque keys `t1…tN` the model drifts to the first keys when unsure, and `notes`, generated
last, could only describe a choice already made. Two changes, both on by default:

- **Reasoning first:** `notes` is the first property of the answer (≤ 300 characters), so the
  choice is generated after, and conditioned on, the model's own reading of the message.
- **Named targets:** each candidate writes `target_name` (a const `"<name> [kind]"`) before
  `target`, so the model picks between meaningful words instead of bare keys.

`tools/benchmark_llm.py --no-reasoning-first --no-target-names` reproduces the old behaviour.

| cases | setting | target | all | safe | wrong | p50 |
|---|---|---|---|---|---|---|
| real workspace (19, private) | baseline | 58% | 32% | 42% | 8 | 32.4 s |
| real workspace (19, private) | both on | 74% | 37% | 47% | 7 | 24.0 s |
| synthetic (44) | Run 3 | 98% | 82% | 91% | 2 | 15.9 s |
| synthetic (44) | both on | 98% | 84% | 89% | 1 | 32.1 s* |

The real-workspace set replays a captured copy of the owner's own workspace
(`tools/capture_workspace.py`, kept under the git-ignored `data/eval/`) with expectations not
yet reviewed by the owner; the wrong target — the failure that prompted this — fell from 8 to 5
cases, and the stray `Databases` pick from 3 to 0. Most of what still fails there is
tags: tasks live in one TODO database, and pages like `дом`/`gnezdo` are views of it filtered
by a tag, which the model cannot know (planned: marking views on the admin page).

On the synthetic set the change is within run-to-run noise (1–2 cases): no regression.
\*Latency is not comparable: the test suite ran on the same machine during this run. Writing the
reasoning first adds up to ~100 generated tokens, so some slowdown is expected and is still
to be measured on an idle machine.

## Run 5 (2026-09-19) — Claude Haiku 4.5

Claude answers in a flat shape (`app/llm/claude.py`): its structured outputs refuse schemas with
more than 16 union-typed parameters, and the per-request schema has one branch per target and
four per field (84 on the real workspace). The flat answer is converted back and checked with
`jsonschema` against the full per-request schema, so the guarantees match the local grammar.

| cases | model | target | all | safe | wrong | p50 |
|---|---|---|---|---|---|---|
| synthetic (44) | mistral-nemo:12b (Run 4) | 98% | 84% | 89% | 1 | — |
| synthetic (44) | claude-haiku-4-5 | 100% | 93% | 98% | 0 | 3.6 s |
| real workspace (19) | mistral-nemo:12b (Run 4) | 74% | 37% | 47% | 7 | 24.0 s |
| real workspace (19) | claude-haiku-4-5 | 100% | 74% | 95% | 0 | 3.9 s |

On the real workspace, what Haiku still misses is tags it was not allowed to infer (4 cases) and
«запиши пароль…» read as create rather than append. With a key set, Claude is the interpreter
and the local model is the fallback.

## Run 6 (2026-09-19) — option descriptions, hidden views, workspace note

The real-workspace set again, with draft settings made from the owner's own page descriptions:
the five TODO tags described (e.g. `home` = the `дом` page's description), the four pages that
only show TODO filtered by a tag hidden, and a three-sentence workspace note.

| model | setting | target | all | safe | wrong | p50 |
|---|---|---|---|---|---|---|
| claude-haiku-4-5 | Run 5 | 100% | 74% | 95% | 0 | 3.9 s |
| claude-haiku-4-5 | described | 95% | 89% | 89% | 2 | 4.1 s |
| mistral-nemo:12b | Run 4 | 74% | 37% | 47% | 7 | 24.0 s |
| mistral-nemo:12b | described | 74% | 37% | 58% | 6 | 34.7 s |

Haiku now sets the tag in every TODO case; its two misses are arguable expectations rather than
clear errors («надо забрать посылки» tagged `home`, not `personal`; «для кнуба разослать
приглашения…» filed in the club's own events database). The 12B model barely uses the
descriptions — another reason Claude is the primary and the local model only the fallback.
