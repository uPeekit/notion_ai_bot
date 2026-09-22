# Obsidian next to Notion — plan

Goal: for a trial period, every message is written to **both** Notion and an Obsidian vault.
Each system is written in its own way, by its own pipeline, so Obsidian keeps working unchanged
if Notion is switched off later. Two more pieces are in scope: a one-off migration of the
existing Notion data into a restructured vault, and a short plugin set that fits the current
workflow.

## 1. Shape

```
message ─┬─ Notion pipeline (unchanged: structure → interpreter → questions → write)
         └─ Obsidian pipeline: filer (light LLM) → vault write → linker (after the reply)
shared:  voice transcription · web research result · one Undo button · one reply
```

- The two pipelines run concurrently, and neither waits for or depends on the other. A failure
  on one side is one line in the reply, never a failed message.
- Only a **fresh message** feeds the Obsidian pipeline. A button press or a typed answer to a
  Notion question does not: it answers Notion, not a new thought.
- Two switches, `NOTION_ENABLED` and `OBSIDIAN_ENABLED`. Turning Notion off leaves a complete
  Obsidian bot, with nothing in its pipeline defined in terms of Notion.
- The reply names both results in one line each, e.g.
  «Notion: TODO · Obsidian: Задачи → «Забрать посылку»». The two sides may read a message
  differently; during a trial that is part of the comparison.

## 2. The filer (the Obsidian interpreter)

**No questions, ever.** It always writes something. When unsure, it writes to `Inbox.md`.
Interpretation is kept light: the note text stays close to your words and is enriched only when
you ask for it (web research).

**Context** (about 1–2k tokens, against about 9k on the Notion side):
- the vault guide `_bot.md`: your conventions in plain words ("tasks go to …", "books get
  `author`", what each area is for). You edit it in Obsidian; it replaces the Notion
  descriptions.
- the top-level folders, and the tags and property names already in use
- the titles and aliases of notes that match words in the message (from the vault index, §4),
  so it can pick a note to update

**Output**, structured (flat schema, like the interpreter's). A list of up to ~10 actions, so
«добавь эти 5 книг» is one call:

| action | what it does |
|---|---|
| `task` | a Tasks line: text, due, recurrence, tags, area |
| `note` | a new note: folder, title, properties, body |
| `append` | lines added to an existing note, under a named heading if there is one, fitted to the list there (`- [ ]`, bullets, numbers) |
| `update` | **v1**: on an existing note, set or clear properties (`status: Read`), tick, untick or re-date a task line («посылку забрал»), replace one section's content |
| `log` | a line in today's daily note (only if daily notes are used) |

Validated deterministically, as on the Notion side: folders must exist, notes to update must be
in the index, the line to tick must match exactly one open task. A write that fails validation
becomes an `Inbox.md` line, never a guess.

The Obsidian side never searches on its own. «что у меня по дому» is answered from Notion while
Notion is on; after the switch, the filer gets a `search` action over the index.

## 3. The vault writer

- Writes `.md` files straight to disk; Obsidian picks up the changes. It does not need
  Obsidian running and needs no key.
- Atomic writes (temp file + rename). The file's modification time is checked before a write,
  so an edit you are making in Obsidian at the same moment is not overwritten: on a conflict
  the bot writes to `Inbox.md` instead.
- It stays inside the vault root, never touches `.obsidian/`, and **never deletes**.
- Undo is shared with Notion (one button undoes both). A note the bot created moves to the
  vault's `.trash/`. For an append or an update, the previous content comes back from a copy
  kept in `data/`, never inside the vault.
- The vault path lives in `.env` (`OBSIDIAN_VAULT=`). Tests use a temporary vault, and nothing
  in dev tooling points at the real one.

## 4. The vault index and the linker

**Index:** titles, aliases, tags, properties and headings of every note. It is built at startup
from the file system and refreshed from modification times on each message (cheap: no parsing
of unchanged files). The filer, the validator and the linker all read it.

**Linker**, run after the reply so the chat is never slower:
1. **Free pass:** exact title, alias or keyword hits become `[[links]]`, which is what
   Obsidian's own "unlinked mentions" would find.
2. **Haiku pass**, for what that misses: other word forms («Пелевина» → `Пелевин`), people by
   nickname, a line that belongs to a project or area note, a book discussed in a club event.
   Input: the new text plus up to ~50 candidate titles picked by an overlap score. Output:
   `{phrase, note}` pairs, accepted only if the phrase is in the text and the note exists. It
   can link; it never creates or rewrites.
3. Skipped for notes under folders marked private in `_bot.md`.

## 5. Migration: Notion → vault

A one-off tool, `tools/notion_to_vault.py`. It **only reads** from Notion through the API.
It writes into a new, empty vault folder. It can be re-run (it overwrites only what it created
itself, tracked by a `notion:` property). A dry-run report lists every file it would write.

Proposed structure, from what the workspace holds today:

| Notion | Vault |
|---|---|
| **TODO** database (Task, Status, Due, Tags, Repeat, Repeat Days, Trackable) | Tasks-plugin lines. Open tasks go to `Задачи.md`; done and passed ones to `Задачи — архив.md`. Tags become `#home #personal #knub #gnezdo #babki`. `Repeat`/`Repeat Days` become `🔁 every week on Monday, Friday` and so on. `Due` becomes `📅`. `Status` Doing/Pass become custom statuses `[/]`/`[-]`. |
| `Trackable` + the `Days Until` / `Next Date` / `Done Rep` formulas | not copied as data; replaced by queries: a "countdown" block on the home note, sorted by due date |
| Area pages that filter TODO by tag: дом, моя херня, gnezdo, knub, бабосы, пройекты | `Области/<name>.md`: the page's own content, plus a live Tasks query `tags include #home`, which does the job of Notion's filtered view |
| **Books** database | `Книги/<Title>.md`, with `status`, `author: "[[Author]]"` and the created date. A `Книги.base` table view grouped by status. |
| **knub** database (events: book, author, date, URL, image, posted flags) | `Кнуб/События/<date> <book>.md` with `book: "[[…]]"`, `date`, `url`, flags, and the image downloaded. `Кнуб.base` view. |
| **People** (hidden, unused) | skipped, unless you want it |
| медиа (lists: films, podcasts, …) | `Медиа.md`, lists kept as tick boxes |
| Loose pages (recipes, trips, torii, Uncharted, …) | `Заметки/<title>.md`, with images downloaded into `Вложения/` |
| дашборд (hidden) | `Главная.md`: today's and overdue tasks, countdowns, books in progress, set as the start page |
| Databases, Корень воркспейса | not needed: folders take their place |
| **прост** ("tokens, passwords") | **not migrated.** A vault syncs to the phone as plain text files; secrets belong in a password manager. You move these by hand. |

Converting Notion blocks to markdown is the reverse of the bot's existing markdown → blocks
code. That covers headings, lists, tick boxes, quotes, code, callouts (→ Obsidian callouts),
toggles (→ foldable callouts), images and links. Links to other migrated pages become
`[[wikilinks]]`. Notion image URLs expire within an hour, so images are downloaded during the
run. Finally, the linker's free pass runs once over the whole vault.

## 6. Plugins

**Core** (built in; switch on):
- **Bases**: tables over notes (Books, Knub). Replaces Notion databases.
- **Properties**, **Templates**, **Backlinks**, **Outgoing links**.
- **Daily notes**: only if you want the `log` action. The flow does not need it.

**Community**:
- **Tasks** (essential): due dates, recurrence (`🔁`), custom statuses (Doing/Pass), queries
  for the area notes and the home note. Covers TODO, Repeat and Trackable.
- **Homepage**: opens `Главная.md` on start, like your Notion dashboard.
- **Omnisearch**: much better search, including Cyrillic word forms.

Optional, depending on the answers below: **Calendar** (if daily notes), **Obsidian Git** (if
the vault syncs through git), **Dataview** (only if Bases turns out too limited for the
countdowns; try without it first).

## 7. Order and size

1. **Migration tool** first: you get a real vault to look at and restructure before the bot
   writes into it. Plugins and `_bot.md` get settled at this stage. *1 release.*
2. **Index + writer + filer** (task / note / append / update / inbox), running next to Notion.
   *2 releases:* create and append first, then updates and undo.
3. **Linker** (free pass, then Haiku pass). *1 release.*
4. Later, if you move: the Notion switch, and `search` on the Obsidian side.

**LLM cost:** +1 Haiku call per message (filer) and +1 per write (linker). Roughly doubles
today's Haiku spend, which is still cents a day at your volume. The migration is a single
Haiku linker run over the vault, which can also be skipped.

## 8. Decisions (2026-09-22)

- Vault: `C:\data\obsidian` (new folder), synced through **Obsidian Sync**. The bot's writes
  are uploaded while Obsidian runs on this PC.
- Folder and note names in Russian.
- People (unused Notion template database): not migrated.
- «прост»: stays out of the vault.
- Daily notes: **on**. «сегодня …» messages become a line in today's note (`Дневник/`);
  the Notion side may simply send them to its inbox.
- 2026-09-22: migration done into `C:\data\obsidian` (53 notes, 24 attachments) with
  `tools/notion_to_vault.py`; settings in git-ignored `data/vault_migration.yaml`.

## 9. Original questions

**No new tokens or keys.** The migration reads Notion with the bot's existing integration
token, and the vault is a local folder.

1. **Vault path** on this PC (a new folder is best for the migration).
2. **How it reaches your phone**: Obsidian Sync, iCloud/OneDrive, Syncthing or git. With
   Obsidian Sync, the bot's writes are uploaded only while Obsidian runs on this PC.
3. **Share the pages you want migrated with the bot's integration** in Notion, if some are not
   shared yet. Discovery only sees shared pages.
4. **Names**: Russian folder names as above, or English?
5. **People**: skip or migrate? **Daily notes**: yes or no?
6. Confirm that «прост» stays out of the vault.
