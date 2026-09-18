# notion_ai_bot

A Telegram bot that turns a text or voice message into a change in your own Notion workspace —
"купи молоко" becomes a row in your shopping list, "в идеи: ..." becomes a paragraph on your
ideas page, and so on. Everything runs on your own machine: speech recognition (faster-whisper)
and the language model (a local [Ollama](https://ollama.com) model) are both local; only the
Notion API call itself leaves the machine. Anything the bot can't confidently classify gets a
follow-up question with buttons — never a silent guess — and, if you flag one, an "inbox" page
that nothing is ever dropped into oblivion for.

Design and internals live in [`documentation/`](documentation/) (start with
[ARCHITECTURE.md](documentation/ARCHITECTURE.md) and [FLOWS.md](documentation/FLOWS.md) if you
want to know *why* something works the way it does). This file is the operator's guide: install
it, configure it, run it, and know where to look when something goes wrong.

## What it understands

Send the bot a plain Russian sentence, as text or as a voice note — no command syntax, no fixed
phrasing:

- **"купи молоко в Рими"** → a new row in your shopping-list database, with whatever properties
  (shop, category, …) it could confidently read off the sentence.
- **"отметь молоко купленным"** → finds the matching row and updates it.
- **"в идеи: попробовать сыр с плесенью"** → appends a paragraph to a page.
- **"что у меня в покупках на Rimi?"** → searches and lists matches.
- Anything genuinely ambiguous ("добавь хлеб", when you have both a shopping list and a task
  list) gets a question back with buttons, never a guessed answer.
- Anything it can't classify at all ("расскажи анекдот") is saved to whichever Notion
  page/database you flagged as the **inbox**, if any, so nothing you sent is ever silently lost —
  see "Flagging the inbox page" below.

Every write comes back with an **Undo** button that works for a few minutes
(`UNDO_WINDOW_S`, see below). The bot never deletes or bulk-edits anything, and it never sees or
uses your Notion access token for anything other than talking to the Notion API.

## Install

You need three things before the bot can run: a place for it to read/write in Notion, a Telegram
bot of your own, and a local language model. None of this touches the bot's code — it all lands
in one `.env` file.

1. **Install [uv](https://docs.astral.sh/uv/)** (`powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`) if you don't already have it, and **install
   [Ollama](https://ollama.com)**.
2. **Pull the local model:**
   ```powershell
   ollama pull mistral-nemo:12b
   ```
   `mistral-nemo:12b` is the default (`LLM_MODEL` in `.env`). This project's own benchmark
   picked it: it makes a third as many confidently-wrong writes as the 8B alternatives, which
   is the failure that costs you cleanup in Notion. It answers in ~16 s on an 8 GB laptop GPU.
   If you'd rather have ~8 s and accept more mistakes, `ollama pull qwen3:8b` and set
   `LLM_MODEL=qwen3:8b`. See [documentation/BENCHMARK.md](documentation/BENCHMARK.md) for the
   numbers behind both.
3. **Get a Notion token.** Follow [documentation/NOTION_SETUP.md](documentation/NOTION_SETUP.md)
   — two minutes, a personal access token from Notion's developer portal. This is what lets the
   bot see and write to your workspace; nothing is shared until you put the token in `.env`.
4. **Create a Telegram bot and find your own user id:**
   - Message [@BotFather](https://t.me/BotFather) on Telegram → `/newbot` → follow the prompts →
     copy the token it gives you.
   - Message [@userinfobot](https://t.me/userinfobot) (or any similar "what's my id" bot) to get
     your own numeric Telegram user id. Without this the bot will not talk to you — see
     `TELEGRAM_ALLOWED_USER_IDS` below.
5. **Build and install a release.** Double-click **`release.cmd`** in this repo and accept the
   defaults (see [RELEASE.md](RELEASE.md)). When it asks *"Update the production install now"*,
   say yes: with no install there yet, it offers a fresh one in `C:\apps\notion_ai_bot` — its own
   Python environment, the database schema, and a `.env` copied from `.env.example`, which it
   offers to open in Notepad. It does **not** fill in your tokens for you — do that next.
6. **Fill in `.env`** in the install directory (`C:\apps\notion_ai_bot\.env` in the example
   above) with the Notion token, the Telegram bot token, and your Telegram user id (see the table
   below for exactly which keys).
7. **Start the bot:** double-click **`start.cmd`** in the install folder
   (`C:\apps\notion_ai_bot\start.cmd`). The bot runs in that window; stop it with Ctrl+C (then
   `Y`) or by closing the window. The first thing it does is discover your workspace and write
   `data\targets.yaml` — watch for `discovery: N targets` to confirm it saw what you expected. If
   it stops instead, the window says why in one line and offers the fix (open `.env`, or apply a
   pending migration); the exit codes are under "When something breaks" below.

   Only one copy runs per install: a second `start.cmd` refuses with "already running", because
   two copies would fight over the same Telegram bot and database. `deploy\run.ps1` still exists
   for unattended starts (Task Scheduler and the like) — it has no prompts.

To upgrade later, see [RELEASE.md](RELEASE.md) — `update.cmd` in the install directory walks you
through it.

## The `.env` keys that matter day to day

Everything below lives in `.env` in the install directory. `.env.example` documents every key
with a default; these are the ones worth knowing about once the bot is running:

| Key | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — (required) | From @BotFather. |
| `TELEGRAM_ALLOWED_USER_IDS` | — (required) | Comma-separated numeric Telegram user ids. Anyone else's message is silently ignored — no reply, nothing written anywhere. Add a second id here to let another person use the same bot. |
| `NOTION_TOKEN` | — (required) | Your personal access token (or internal-connection secret) — see [NOTION_SETUP.md](documentation/NOTION_SETUP.md). |
| `LLM_MODEL` | `mistral-nemo:12b` | The Ollama model tag. Must be pulled (`ollama pull <tag>`) — a missing model only warns at startup, it doesn't stop the bot, so a typo here means every message just fails until you fix it. |
| `LLM_KEEP_ALIVE` | `30m` | How long Ollama keeps the model loaded after a message. Loading it takes ~16 s, so the first message after a longer pause answers in ~32 s instead of ~16 s. `-1` keeps it loaded for good — always fast, but the model then holds your GPU (and on a laptop, some battery) even while you don't use the bot. |
| `OLLAMA_BASE_URL` | `http://127.0.0.1:11434` | Where Ollama is listening. Point this at another machine's Ollama on your network to run the model somewhere else — see below. |
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` / `WHISPER_LANGUAGE` | `large-v3-turbo` / `auto` / `int8` / `ru` | Speech recognition. `WHISPER_DEVICE=auto` tries your GPU first and falls back to CPU automatically (and remembers that decision for as long as the process runs). **`.env.example` ships `cpu`**, because the default 12B model uses all 8 GB of an RTX 4070 Laptop and leaves nothing for Whisper — on a bigger GPU, or with `LLM_MODEL=qwen3:8b`, `auto` is fine. |
| `INBOX_MODE` | `auto` | `auto` saves an unresolved message immediately and still offers `[Отменить]`/`[В разное]`; `button` only saves when you press `[В разное]`; `off` disables the inbox entirely. |
| `INBOX_TARGET_ID` | (empty) | Overrides the admin page's inbox pick — see below. Leave empty and use the admin page instead unless you have a reason to hard-code a target id. |
| `ADMIN_UI_PORT` | `8787` | The local admin page's port (`http://127.0.0.1:8787`). Set to `0` to disable the admin page entirely. |
| `SESSION_TTL_S` | `900` (15 min) | How long a clarifying question stays open before the bot gives up on it (and, in `auto`/`button` inbox mode, rescues the original text to the inbox instead of leaving it unanswered). |
| `UNDO_WINDOW_S` | `300` (5 min) | How long the `[Отменить]` button on a reply keeps working. |
| `LOG_LEVEL` | `INFO` | See "Logging", below. |
| `POLICY_INTENT_MIN`, `POLICY_TARGET_MIN`, `POLICY_TARGET_MARGIN`, `POLICY_FIELD_MIN`, `POLICY_DATE_MIN` | `0.85` / `0.85` / `0.10` / `0.75` / `0.80` | How cautious the bot is before acting without asking — see "Tuning how cautious the bot is", below. |

## Flagging the inbox page

Anything the bot can't resolve — a message it can't parse, a question nobody answered in time,
a write Notion itself rejected — is appended to one Notion page or database you designate as the
**inbox**, instead of being dropped. Nothing is flagged as the inbox by default.

To flag one: open **`http://127.0.0.1:8787`** in a browser on the same machine the bot runs on
(this page only ever answers requests from that machine — it is not reachable from your phone or
another computer). Pick any target's radio button under "инбокс — сюда бот складывает всё, что не
смог распознать" and click **Сохранить**. That's it; no restart needed.

If the radio buttons are greyed out with a notice about `INBOX_TARGET_ID`, that env var is set and
overrides whatever you'd pick here — either unset it in `.env` and restart, or edit it directly if
you'd rather hard-code a target id (a Notion page or database id) than use the page. See
[NOTION_SETUP.md](documentation/NOTION_SETUP.md) ("Inbox target") for the full explanation,
including `INBOX_MODE`'s three settings.

## Telegram commands

| Command | Does |
|---|---|
| `/start`, `/help` | Prints a short usage reminder. |
| `/undo` | Reverts the most recent change in this chat, if the undo window hasn't closed. |
| `/cancel` | Drops whatever clarifying question is currently pending, without answering it. |
| `/refresh` | Re-scans your Notion workspace right now instead of waiting for the normal cache (see `SCHEMA_CACHE_TTL_S` in `.env.example`) and replies with how many targets it found. |
| `/targets` | Lists every page/database the bot currently sees, with the inbox one marked. |

All six are published to Telegram at startup, so they show up in the "/" menu in the chat rather
than having to be remembered.

Besides commands, just talk to it — text or a voice note, in Russian. A voice note is transcribed
locally and never echoed back to you; you get a short «Расшифровываю…» acknowledgement while that
runs, and the reply always describes the actual Notion change, not the transcript.

## Reading the audit log

Every message the bot handles — successful or not — writes exactly one row to the `events` table
in `data\bot.sqlite` (in the install directory), whether or not anything ended up in Notion. This
is the place to look when you want to know *why* the bot did (or didn't do) something, days later.
Useful columns: `ts`, `raw_input` (your text, or the voice transcript), `decision` (EXECUTE /
CLARIFY / REJECT and why), `error` (the code, if any — cross-reference against
[ERRORS.md](documentation/ERRORS.md)), `notion_page_id`, `duration_ms`.

Query it with the `sqlite3` CLI, or open it with a free GUI tool like
[DB Browser for SQLite](https://sqlitebrowser.org/) if you'd rather click around than type SQL:

```powershell
sqlite3 C:\apps\notion_ai_bot\data\bot.sqlite "SELECT ts, kind, decision, error FROM events ORDER BY id DESC LIMIT 20;"
```

Your Notion token and Telegram bot token are never written to this database, or to any log —
see "Logging" below and `documentation/ARCHITECTURE.md` §14.

## Tuning how cautious the bot is

Five thresholds in `.env` (0–1, see the table above) decide when the bot acts on its own versus
asking a clarifying question first. Roughly:

- **`POLICY_INTENT_MIN`** / **`POLICY_TARGET_MIN`** — how sure the model has to be about *what
  you want done* and *which Notion page/database it belongs in* before it just does it. Lower
  these to get fewer "which of these did you mean?" questions (at the cost of more wrong guesses);
  raise them to get more questions and fewer mistakes.
- **`POLICY_TARGET_MARGIN`** — how much clearer the best guess has to be than the second-best
  guess. Lower it if the bot keeps asking to choose between two targets that are obviously not
  both right; raise it if it's picking the wrong one of two similar targets without asking.
- **`POLICY_FIELD_MIN`** / **`POLICY_DATE_MIN`** — the same idea for individual field values and
  dates specifically (dates get their own, usually higher, threshold because a wrong date is
  easy to say confidently and easy to get wrong).

There's no need to guess blindly: `data\bot.sqlite`'s `events.candidate_scores` and
`validation_result` columns (see "Reading the audit log") show you the actual confidence numbers
the bot saw for messages you've already sent, so you can see exactly which threshold a real
message tripped before changing it. Restart the bot after editing `.env` for a new value to take
effect.

## Running Ollama or Whisper on another machine

**Ollama already supports this today.** Set `OLLAMA_BASE_URL` in `.env` to point at any reachable
Ollama server (e.g. `http://192.168.1.20:11434` for another PC on your network with a better GPU)
and restart the bot — nothing else changes.

**Whisper (speech recognition) does not, yet.** There's a `WHISPER_DEVICE=cpu` setting for running
it *without a GPU on the same machine*, but there is currently no way to point speech recognition
at a different machine — the interface for that (`app/speech/whisper_remote.py`) hasn't been
built. If your machine has no usable GPU, voice messages still work, just more slowly (CPU
`int8`); text messages are unaffected either way.

## Logging

The bot logs to its console window **and** to `logs\bot.log` in the install folder, rotated at
5 MB with five old files kept (`bot.log.1` … `bot.log.5`), so the log survives closing the window
and never grows without bound. Change the location with `LOG_FILE` in `.env`; set it empty to
switch the file off. At the default `LOG_LEVEL=INFO` you'll see one line per notable
event (discovery results, warnings, errors) — never your message text, and never either token,
however verbose you make it. That second part isn't just "we try not to log tokens": every line
the bot writes is passed through a filter that replaces your Telegram and Notion token values
with `***` first, whoever produced the line — in the file exactly as on screen. On top of that, Telegram's
own client library normally puts your bot token straight into the URL of every request it makes,
and logs that URL — so the bot deliberately holds two of that library's own loggers (`httpx`/
`httpcore`, its HTTP layer, and `telegram.ext.ExtBot`, which logs the same URL once on its own at
`DEBUG`) at `WARNING` no matter what `LOG_LEVEL` you set, specifically so raising it to `DEBUG` to
chase down a problem can never turn either leak back on. See `app/logging_setup.py` and
`documentation/ARCHITECTURE.md` §14 for the full detail.

## When something breaks

If the process **exits immediately** instead of starting up, the exit code tells you what to fix
— see [documentation/ERRORS.md](documentation/ERRORS.md) ("Startup checks") for the full list:

| Exit code | Means |
|---|---|
| `2` | A `.env` value is missing or unusable (it names which key, never the value). Three ways to get it: a required key missing; a `LOG_LEVEL` that isn't one of `CRITICAL`/`ERROR`/`WARNING`/`INFO`/`DEBUG`; or Telegram itself rejecting `TELEGRAM_BOT_TOKEN` (mistyped, or revoked/regenerated in @BotFather since you last pasted it). |
| `3` | Notion rejected the token outright (401/403) — check `NOTION_TOKEN`. |
| `4` | The database has a pending schema migration. `start.cmd` offers to apply it on the spot (backing the database up first); otherwise run `.venv\Scripts\python.exe -m tools.migrate --apply` in the install folder. |
| `5` | The bot is already running for this install — look for its other window. |

**The first voice message takes minutes and looks like nothing is happening.** That is expected
once: the speech model (`large-v3-turbo`, ~1.5 GB) is downloaded from HuggingFace on first use
and cached under your user profile, and the console shows download progress bars while it
happens. The bot replies «Расшифровываю голосовое сообщение…» as soon as the voice note arrives
so you can tell it is alive; the real answer follows when the model has loaded. Every later voice
message reuses the loaded model and is fast.

**A voice message kills the whole bot process on an NVIDIA machine** — the console shows
something like `Could not locate cudnn_ops64_9.dll` and the process disappears with no Python
traceback. That abort happens inside the CUDA library, below Python, so the bot's own
CPU fallback never gets the chance to catch it. Either install the cuDNN 9 runtime for your CUDA
version, or set `WHISPER_DEVICE=cpu` in `.env` and restart — voice messages then work on the CPU,
just more slowly, and nothing else changes.

**The admin page isn't there but the bot works.** Look for a `admin page could not bind
127.0.0.1:8787` warning in the console: something else is on that port, or Windows has reserved
it (`WinError 10013`; `netsh interface ipv4 show excludedportrange protocol=tcp` lists the
reserved ranges). Pick another port with `ADMIN_UI_PORT` and restart. The bot itself keeps
running either way — the page is optional.

A failure that only **warns** (Ollama unreachable, a model not pulled, Notion briefly
unreachable, no inbox target flagged, the admin port unavailable) does not stop the bot — it keeps running and simply can't do
that one thing until you fix it. `documentation/ERRORS.md` is the full table of every error code,
what the user sees, and what the bot does about it; it's worth a look before assuming something
is broken versus working as designed (a clarifying question is not an error).

## Quick smoke test after installing

Once `.env` is filled in and the bot is running:

1. Flag a page as the inbox on `http://127.0.0.1:8787` (see above).
2. Text: «купи молоко» → a row appears in your shopping list, the reply names it, `[Отменить]`
   works.
3. The same sentence as a voice note → the same result.
4. Something deliberately vague, e.g. «добавь хлеб» when you have more than one place it could go
   → a question with buttons; «расскажи анекдот» → filed to «В разное» if you flagged an inbox.

## Development

This repo (`uv sync`, `uv run pytest -q`, `uv run ruff check .`) is the development tree — the
bot is only ever meant to be *run* from a release install, never from here. See
[RELEASE.md](RELEASE.md) for building and shipping a release, and `documentation/` for how the
whole thing is put together.
