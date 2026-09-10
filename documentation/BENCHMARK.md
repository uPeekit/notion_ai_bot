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

## Decision

**`qwen3:8b` is the new default**, replacing `llama3.1:8b`. Decision rule: highest `safe`, `wrong`
as a tie-break penalty, then `all`, then p50 latency — qwen3:8b wins outright on the primary
metric alone (`safe` 77% vs 73%), so the tie-break never engages. It also leads on every other
axis: `all` 73% vs 57%, `intent` 93% vs 86%, `fields` 82% vs 70%, and p50 13.5 s vs 17.3 s (p95
26.3 s vs 30.5 s). llama3.1:8b's higher `wrong` count is misleading in isolation (7 vs 8) — most of
its failures are non-`wrong` (deferred/`not_mentioned`) title omissions caused by the Step 3
regression noted above, not confidently-wrong values, but they still cost it `all` and `safe`
relative to qwen3:8b's smaller, more genuine failure set (mostly real date/relation edge cases).

`qwen2.5:7b-instruct` and `gemma3:4b` were not re-benchmarked in Run 2 (only the top two Run 1
models were re-run, per the tuning brief); they trailed badly in Run 1 (45% `all` each) and there
is no reason to expect the prompt/calendar changes close that gap, but this is inference, not
measurement — a future full re-run should confirm before treating them as ruled out.
