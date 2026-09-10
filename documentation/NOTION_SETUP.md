# Notion access setup

The bot talks to Notion with one bearer token in `NOTION_TOKEN`. Notion offers two token kinds; pick one.

| | Personal access token (recommended) | Internal connection |
|---|---|---|
| Acts as | you, with your permissions | a separate "connection" identity |
| What the bot sees | everything you can see in the chosen workspace | only pages you explicitly connect (children inherit) |
| Setup effort | 2 minutes, no per-page sharing | create connection + share each top-level page |
| Requires | any member | workspace owner |

The bot is designed for "everything available", so a personal access token fits. Use an internal connection if you want to fence the bot to a few pages.

## Option A — Personal access token

1. Open https://www.notion.so/developers (Developer portal). Sign in with the account that owns the workspace.
2. Go to **Personal access tokens** → **New token**.
3. Name it (e.g. `telegram-bot`), select the workspace, tick the **Notion API** capability, click **Create token**.
4. Copy the token immediately; Notion will not show it again.
5. Put it in `.env`:
   ```dotenv
   NOTION_TOKEN=<paste>
   ```
6. Verify:
   ```powershell
   uv run python -m tools.discover
   ```
   You should see your pages and databases as a tree. Exit code 3 means the token is invalid; 4 means another Notion error (the message shows the code).

Notes:
- Business/Enterprise workspaces: a workspace owner must allow personal tokens under **Settings → Connections** first.
- Revoke any time in the Developer portal; the bot then gets 401 and stops.

## Option B — Internal connection

1. Open https://app.notion.com/developers/connections (Developer portal → **Connections**). You must be a workspace owner.
2. **New connection** → type *Internal* → name it → select the workspace → save. Copy the secret shown on the connection page into `NOTION_TOKEN`.
3. In the Notion app, open every top-level page (or database) the bot may use → `•••` menu top-right → **Add connections** → pick your connection. Access flows down to all child pages and databases under that page, so connect parents, not every page.
4. Verify with `uv run python -m tools.discover`. Pages you forgot to connect simply do not appear.
5. Manage or revoke later under **Settings → Connections → Manage**.

## What the bot does with access

- Discovery calls `search` once per message (cached 60 s) and reads every visible database schema and its newest rows (`ITEMS_PER_TARGET`, default 15).
- Writes happen only through validated commands: create/update database rows, create sub-pages, append paragraphs, archive (for Undo). No deletes, no bulk operations.
- The token never leaves the app process: it is not logged, not stored in SQLite, and never shown to the LLM.

## Descriptions for the LLM

After the first `tools.discover` run, `data/targets.yaml` lists every visible target. Add a one-line `description` per database/page (what goes there, how you phrase it) and set `required: true` on fields the bot must always ask for. Notion's own database description is used when yours is empty.
