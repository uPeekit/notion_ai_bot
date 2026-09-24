# Android widget for markdown tasks — specification

A free Android app that reads a plain Obsidian vault and gives you, on the home screen, what
the Obsidian mobile app cannot: today's and overdue tasks, a calendar of dated items, and real
notifications. No bot, no server, no account — it reads the same files Obsidian does.

This is a design document for a future project, not for the Telegram bot. Move it to the
widget's own repository when that starts.

## 1. Why: what exists today (checked 2026-09-25)

| What | State |
| --- | --- |
| [Android-Markdown-Widget](https://github.com/Tiim/Android-Markdown-Widget), [obsidian-todo-widget](https://github.com/YukiGasai/obsidian-todo-widget), [Obsidian-Note-Widget](https://github.com/kleokl7/Obsidian-Note-Widget), XJ-Ong, saviorand | render **one configured file**; no vault scan, no dates, no notifications; APK from GitHub |
| TaskForge (Play Store) | does all of it — widgets, reminders, calendar, custom lists are **premium** |
| obsidian-reminder (plugin) | fires only while Obsidian is open; useless for a locked phone |
| Tasks.org, SimpleReminder, reReminder | good apps, own database, do not read a vault |

**The gap:** a free, vault-wide, date-aware widget with notifications. Nothing occupies it.

## 2. Scope

**v1 does:** index a vault; a list widget (today / overdue / next 7 days); a month calendar
widget; notifications for dated tasks; tap to complete, with correct recurrence; quick-add a
task to a chosen note.

**v1 does not:** edit note text, render full markdown, sync anything, create notes, parse
Dataview queries, or replace Obsidian on the phone.

## 3. Data model

```
Task: file, line, statusChar, description, tags[], priority,
      due?, scheduled?, start?, created?, done?, cancelled?,
      recurrence?, reminderTime?, blockId?
Event: file, title, date, endDate?, props{}          // a note with a date property
Status: char, name, type (TODO|DONE|IN_PROGRESS|CANCELLED|NON_TASK), nextChar
```

## 4. Parsing

### 4.1 Task lines

```
^(\s*)([-*+]|\d+[.)])\s+\[(?<status>.)\]\s+(?<body>.*)$
```

From `body`, strip and capture, in any order and anywhere on the line:

| Field | Tasks (emoji) | Dataview inline |
| --- | --- | --- |
| due | `📅 YYYY-MM-DD` | `[due:: YYYY-MM-DD]` |
| scheduled | `⏳` | `[scheduled:: …]` |
| start | `🛫` | `[start:: …]` |
| created | `➕` | `[created:: …]` |
| done | `✅` | `[completion:: …]` |
| cancelled | `❌` | `[cancelled:: …]` |
| recurrence | `🔁 <rule>` | `[repeat:: <rule>]` |
| priority | `🔺 ⏫ 🔼 🔽 ⏬` | `[priority:: high]` |
| id / depends | `🆔`, `⛔` | `[id:: …]`, `[dependsOn:: …]` |

Support **both** notations: vaults are split between them. What remains after stripping is the
description; `#tags` are collected from it and kept in place.

**Do not treat as tasks:** lines inside fenced code blocks, inside frontmatter, and files under
`.obsidian/`, `.trash/`, and the user's template folder. **Do** parse tasks inside callouts and
nested list items — they are ordinary tasks to Obsidian.

### 4.2 Statuses

Read `.obsidian/plugins/obsidian-tasks-plugin/data.json` when present: it holds the user's
custom statuses (`char`, `name`, `type`, `nextStatusChar`) and the **global filter**. Honour
both, or the widget will show a different task list than Obsidian does — the fastest way to
lose a user's trust. Fall back to: `' '` = TODO, `'x'`/`'X'` = DONE, `'/'` = IN_PROGRESS,
`'-'` = CANCELLED, anything else = TODO.

### 4.3 Recurrence

Subset that covers almost all real vaults:

```
every day | every N days | every weekday
every week [on Monday[, Friday]] | every N weeks
every month [on the Nth | on the last] | every N months
every year
… [when done]
```

Parse into an RRULE-like structure. `when done` means the next occurrence is computed from the
completion date instead of the previous due date. Anything unparsed: keep the text verbatim,
copy it to the next occurrence untouched, and do not try to be clever.

### 4.4 Dates with a time

The Tasks plugin stores **dates only**, which is a problem for reminders. Sources of a time, in
order: an explicit `⏰ HH:MM` or `[time:: HH:MM]`; the obsidian-reminder syntax `(@YYYY-MM-DD
HH:MM)`; a leading `HH:MM` or `H.MM` in the description (very common in practice); otherwise a
user-configured default reminder time per list (e.g. 09:00).

### 4.5 Notes as events

For the calendar, also index notes whose frontmatter carries a date in a configurable property
(`date`, `due`, `start`, `when`). This is how meetings, birthdays and club events are stored.

## 5. Index and performance

- **Storage access: SAF** (`ACTION_OPEN_DOCUMENT_TREE`, persisted tree URI).
  `MANAGE_EXTERNAL_STORAGE` is a restricted Play permission and will probably be rejected for a
  widget app. SAF costs speed, so the index is not optional.
- **Room database**: `files(uri, name, mtime, size, hash)`, `tasks(...)`, `events(...)`.
- **Incremental scan**: list the tree, compare `mtime`/`size`, re-parse only what changed.
  Full scan on first run and on "rescan" only. Budget: a 5 000-note vault should re-scan in
  under a second when nothing changed, and parse at ≥ 2 MB/s on a mid-range phone.
- **Triggers**: WorkManager every 15–30 min, on widget tap, on app open, after a write, and on
  `BOOT_COMPLETED`. `FileObserver` does not work through SAF — do not design around it.

## 6. Writing back

Completing a task from the widget is the only write in v1, and it is where data loss would come
from. Algorithm:

1. Re-read the file *now* (never trust the index).
2. Find the stored line **by content**, not by line number. Not found, or found twice → abort,
   rescan, tell the user.
3. Build the new text: set the status char to the status's `nextStatusChar`, append
   `✅ <today>` (or `❌` for cancelled) per the plugin's convention.
4. If the task recurs: insert the next occurrence **above** the completed line (the plugin's
   default), with dates shifted by the rule.
5. Write the whole file back atomically (SAF: truncate + write in one `ParcelFileDescriptor`
   session), preserving the original line endings and trailing newline.
6. Update the index from what was written.

Never rewrite a file to normalise formatting, never reorder lines, never touch frontmatter.
The user's file must differ only in the lines that changed.

## 7. Notifications

- `POST_NOTIFICATIONS` runtime permission (Android 13+).
- Exact alarms: `SCHEDULE_EXACT_ALARM` is restricted; a task reminder qualifies for
  `USE_EXACT_ALARM` on Android 14+, but be ready to fall back to `setWindow()` inexact alarms,
  and explain that in the Play data form.
- Schedule only the next N (say 50) upcoming reminders, not one alarm per task in the vault;
  reschedule after each scan and on boot.
- Actions on the notification: Done, Snooze 1h, Open in Obsidian (`obsidian://open?vault=…&file=…`).
- A daily "agenda" notification at a configured hour is the feature most people actually want.
- Warn the user when the app is battery-optimised: alarms will drift.

## 8. Widgets

- **Agenda list widget** (`RemoteViewsService` + collection): sections Overdue / Today /
  Tomorrow, checkbox tap → complete, header tap → open the app, configurable filter per widget
  instance (tags, folder, list).
- **Month calendar widget**: dots or counts per day from tasks + note-events; tap a day → that
  day's list.
- **Single-note widget**: the existing open-source niche, cheap to add, good for a daily note.
- Widget configuration activity per instance; several widgets with different filters is the
  feature TaskForge charges for.
- Respect dark mode and dynamic colour (Material You); keep tap targets ≥ 48 dp; expect
  `RemoteViews` to support only a small set of views — no WebView, no arbitrary markdown
  rendering. Render tasks as plain text with the emoji fields hidden by default.

## 9. Play Store risks

| Risk | Mitigation |
| --- | --- |
| Storage permission rejected | ship SAF only; never request `MANAGE_EXTERNAL_STORAGE` |
| "Obsidian" in the app title | name it for what it does ("Markdown Tasks Widget"); mention compatibility in the description; do not use the Obsidian logo |
| Exact-alarm policy | justify as user-set reminders; degrade gracefully |
| Data safety form | "no data collected, no data shared" — true, and a selling point |
| Target SDK deadlines | plan for a yearly bump; an unmaintained app is delisted |
| Background restrictions per OEM (Xiaomi, Samsung) | onboarding screen that opens the battery-optimisation settings |

## 10. Risks that are not Play's

| Risk | Why it matters | Mitigation |
| --- | --- | --- |
| **Corrupting someone's notes** | the one bug nobody forgives | content-matched writes, atomic write, a "backup before write" option, extensive fixtures |
| **Sync races** (Syncthing, Obsidian Sync, Dropbox) | the file changes under you mid-edit | re-read before write; abort on mismatch; never hold a file open |
| Large vaults | 10 000 notes over SAF is slow | index, incremental scan, never parse on the UI thread |
| Format drift | the Tasks plugin adds fields | parse leniently, keep unknown tokens verbatim, round-trip tests |
| Vault variety | Dataview, TaskNotes, org-style users | support both notations; say plainly what is not supported |
| Battery blame | widgets get uninstalled for it | one periodic job, coalesced work, no foreground service in v1 |
| Maintenance | a free app still costs weekends | keep v1 small; no accounts, no server, no sync of your own |

## 11. Test corpus

Fixtures the parser must survive, each with an expected parse:

both date notations · every status char including unknown ones · recurrence with and without
dates · `when done` · tasks in callouts, nested lists, numbered lists · tasks inside code fences
(must be ignored) · frontmatter that looks like a task · emoji in the description itself ·
`#tags/with/slashes` · Cyrillic and emoji file names · CRLF files · a file with no trailing
newline · a 2 MB daily note · a task line 4 000 characters long · duplicate identical lines in
one file (the write-back must refuse).

## 12. Roadmap

1. **v0.1** — folder picker, index, agenda list widget, read-only.
2. **v0.2** — tap to complete, with recurrence and the safe-write algorithm.
3. **v0.3** — notifications and the daily agenda.
4. **v0.4** — month calendar widget, per-widget filters.
5. **v1.0** — Play release, no accounts, no tracking, free.
