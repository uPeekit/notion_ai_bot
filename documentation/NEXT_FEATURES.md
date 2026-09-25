# Three things to build next — design

> **Status: all three shipped.** Kept as the design record and the reasoning behind the
> choices; what the code actually does is in ARCHITECTURE.md §14a and FLOWS.md F22–F24.
> Two places where the build deviated from this design, both deliberate:
>
> * **A `reason` attribute rather than an `LLMNoCredit` class.** One mechanism covers all
>   four causes (no credit, a rejected key, a rate limit, an outage) instead of a subclass
>   for one of them.
> * **No `heading` field on the Notion rewrite.** The instruction is free text and the
>   rewriter sees the whole page, so «перепиши раздел про размеры» works without a
>   schema field the interpreter would have to fill. The vault side, whose filer already
>   names headings, does take one.

Written 2026-09-26, after a session that surfaced all three: a plan whose enrichment reached
only Notion, a request to rewrite a page that was refused, and a bot that silently switched to
the local model because the Anthropic account had run out of credit.

Order: **A** first (it is small and it makes every other failure legible), then **B**, then
**C**.

---

## A. Say plainly when Claude cannot be used

### What happens today

`claude 400: Your credit balance is too low…` becomes `LLMUnavailable`, the fallback switches
to the local 12B model for five minutes, and the reply looks ordinary — only worse. The
Obsidian side does not fall back at all: its line reads `Obsidian: не записано (claude 400)`.
Nothing tells the user that the account needs topping up, and the log line is the only place
the reason exists.

### Design

1. **A distinct error.** `app/llm/base.py` gains `LLMNoCredit(LLMUnavailable)`. The Claude
   client raises it when the API answers 400/402 with a body mentioning `credit balance`,
   `billing`, or `insufficient_quota`; the same test lives in one helper so the filer, the
   planner, the researcher, the section picker and the mail classifier all recognise it.
2. **A shared state object**, `app/llm/health.py`: `Health.claude_down(reason)` records what
   happened and when; `Health.note()` returns a line to show the user, or `""` when it was
   already shown within `HEALTH_REMIND_S` (default 3600 s). One instance, passed where the
   other cross-cutting objects already go (switches, tuning).
3. **The user is told, once an hour, in the reply itself**: «⚠️ Claude недоступен: закончились
   кредиты. Отвечаю локальной моделью — она заметно хуже. Пополнить:
   console.anthropic.com/settings/billing». Other hard failures get their own wording (bad
   key, rate limit, service down) — the point is that the reason is named.
4. **The Obsidian line stops showing raw API errors.** `Obsidian: не записано (claude 400)`
   becomes the same sentence as above, because it is the same cause.
5. **The morning digest and the mail digest carry the note too** when they are affected: a
   digest that silently did not run is worse than one that says why.
6. **Startup names it.** `post_init_checks` already pings Claude; a credit failure there logs
   a warning and sends the note with the first reply of the run.
7. **The cooldown grows for billing errors**: five minutes is right for a rate limit, wrong
   for an empty balance. `LLMNoCredit` sets 30 minutes.

### Tests

The classifier (which 400s count), the once-an-hour rule, the Obsidian line, and an end-to-end
turn where Claude has no credit: the reply still writes to Notion through the local model *and*
carries the warning.

---

## B. Rewriting text that is already there

### Goal

«У меня страница про шведскую стенку, там много всего из разных источников — оставь один
подход и одни размеры, сделай один план». Today the Notion side refuses (`SEM_UNSUPPORTED_OP`:
a page supports appends, not updates) and the Obsidian side can only replace a section it can
name.

### The action

A new intent, `rewrite`, on both sides. Three things identify what to rewrite: the place (page
or note), optionally a heading inside it, and the user's instruction in their own words.

```
rewrite(target=«Шведская стенка», heading=None,
        instruction=«оставь один подход, одни размеры, сделай один план»)
```

### How it runs

1. **Read what is there.** Notion: the page's blocks → markdown (the converter written for the
   migration, `app/vault/notion_blocks.py`, moves to `app/notion/to_markdown.py` and is shared).
   Obsidian: the note's text, or one section of it.
2. **One model call**, Sonnet — this is the user's own content being rewritten, and Haiku is
   not good enough for "consolidate three sources into one plan". Input capped at ~20 000
   characters, output at ~8 000. The prompt's rules: keep every fact the instruction does not
   ask to drop, keep the user's language and units, never invent numbers, return markdown only.
3. **Show, then write.** The reply carries the first lines of the new version and the size
   change («было 137 строк → стало 46»), with the usual Undo button.
4. **Write.**
   - *Obsidian*: replace the note (or the section) and keep the previous text in the undo
     record, exactly as every other vault write. A copy of the old note also goes to `.trash`,
     so it survives even after the undo window.
   - *Notion*: append the new blocks, then archive the old text blocks (`DELETE /blocks/{id}`).
     **Images, files, sub-pages and databases are never archived** — only paragraphs, lists,
     headings, quotes and code. The undo record keeps the old blocks' *markdown*, so Undo
     re-appends the text (the block ids do not come back; the content does).

### Why not just delete and re-create the page

Because the page id is referenced by links, by the bot's own targets file, and by anything the
user has in Notion. Replacing contents keeps the page.

### Edge cases

- **Nothing to rewrite** (empty page): refuse with a plain sentence.
- **Too big** (over the input cap): rewrite one section, and say that is what happened.
- **A page that is mostly images**: the text is rewritten, the images stay where they are.
- **A rewrite that shrinks the text by more than 80 %**: allowed, but the reply says so in
  those words, because that is the case where Undo matters most.
- **Two rewrites in a row**: the second one reads what the first produced. Undo still steps
  back one at a time.

### Tests

The block→markdown round trip; the "never archive an image or a sub-page" rule; undo restores
the old text in both stores; the size-change line; a model that returns an empty answer changes
nothing; an instruction that would drop everything still keeps what it was not asked to drop
(pinned by a fixture, not by the model's goodwill).

---

## C. Multi-step work that reaches both stores

### What happens today

A goal («найди то-то, допиши на страницу, потом найди ещё и допиши») becomes a plan: the
planner splits it into steps and the **Notion** pipeline runs them one by one. The Obsidian
side sees the original message once, reads it as a question, and writes nothing. That is why
the enrichment landed in Notion alone.

### Design: the plan is shared, the research is done once

1. The planner stays where it is and keeps producing steps.
2. **Each step is offered to both stores.** The Notion side executes it as now. If the vault is
   on, the same step text goes to `VaultPipeline.handle(step_text, content=…)`.
3. **`content` is the key.** When a step did research, the Notion side already paid for it;
   the text it produced is handed to the vault so the same material is written there, with no
   second search, no second wait and no second bill.
4. **The filer still decides where it goes in the vault.** It is not a mirror: the Notion step
   says "append to Шведская стенка", the filer picks the note by its own index and rules. When
   it cannot place it, the text goes to `Разное.md` rather than nowhere.
5. **Undo covers the whole plan.** The plan's batch undo already collects each step's Notion
   undo; the vault undos join the same batch, so "Отменить всё" reverts both stores.
6. **The progress messages stay as they are** (one per step), with the vault's line appended to
   the step's own reply.

### Then: plans without Notion

Once step 2 is in, `NOTION_ENABLED=false` still leaves plans unavailable, because the planner is
handed a *Notion* workspace summary. The second half of this piece generalises that: the
planner takes a workspace description string, and the vault produces its own — folders, tags,
the guide note, the names of the notes a step might touch. Nothing else in the planner changes.

### Edge cases

- **A step that only searches** produces no vault write (there is nothing to file yet); the
  next step, which writes, carries the content.
- **A step the vault cannot place** goes to the inbox note, and the reply says so.
- **A vault failure inside a plan** never stops the plan: one line, and the plan goes on.
- **Cost**: one extra Haiku call per step, on a small context. A five-step plan is a few cents.

### Tests

A plan where every step reaches both stores; a plan where the vault is switched off; research
content shared rather than searched twice; the batch undo reverting both; a vault failure mid
plan leaving the Notion side intact.

---

## What this does not cover

- Editing a Notion **database row's** properties by rewriting text — the existing update path
  already does that.
- Rewriting inside images, tables or embeds.
- A confirmation step before a rewrite. Undo plus the `.trash` copy is the safety net; if that
  proves too thin in practice, a confirm button is a small addition afterwards.
