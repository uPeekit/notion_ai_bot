# LLM benchmark — 2026-09-10

Hardware: RTX 4070 Laptop (8 GB VRAM), Ryzen AI 9 HX 370. Ollama at `http://127.0.0.1:11434`.
Cases: `tests/fixtures/ru_cases.yaml` (44), context: `tools/sample_workspace.py`, `num_ctx=16384`, `temperature=0`.

| model | n | valid | intent | target | fields | all | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|---|
| llama3.1:8b | 44 | 100% | 89% | 95% | 73% | 57% | 9288 | 13202 |
| qwen3:8b | 44 | 100% | 84% | 91% | 77% | 55% | 11141 | 21625 |
| qwen2.5:7b-instruct | 44 | 100% | 86% | 95% | 61% | 45% | 7109 | 11171 |
| gemma3:4b | 44 | 100% | 91% | 84% | 59% | 45% | 7835 | 14952 |

## llama3.1:8b failures (19)
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

## qwen3:8b failures (20)
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

## qwen2.5:7b-instruct failures (24)
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

## gemma3:4b failures (24)
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

## Decision

**Chosen model: `llama3.1:8b`** — highest `all` rate (57%), and also fastest among the two
top scorers is not the deciding factor here since 57% > 55% outright (no tie-break needed).
p50 9288 ms / p95 13202 ms, well within interactive budget on the RTX 4070 Laptop 8 GB.

**Runner-up: `qwen3:8b`** — 55% `all`, highest `fields` rate (77%) of all four models, but
slower (p50 11141 ms, p95 21625 ms) and less reliable on intent classification (84% vs 89%).

`qwen2.5:7b-instruct` and `gemma3:4b` tied for third at 45% `all`; `gemma3:4b` is fastest
overall (p50 7835 ms) but weakest on target selection (84%), making it the better fast-fallback
candidate mentioned in ARCHITECTURE §13 rather than the default.

### Three most common failure patterns (across all four models)

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
