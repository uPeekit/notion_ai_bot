# notion_ai_bot

Local Telegram → Notion assistant. Voice/text in Telegram, local Whisper + Ollama, deterministic validation, Notion REST.

Design: see `documentation/`.

## Setup

1. Install [uv](https://docs.astral.sh/uv/) and run `uv sync`.
2. Copy `.env.example` to `.env` and fill tokens (see below).
3. `uv run python -m tools.discover` prints what the Notion integration can see.

### Notion access
Follow `documentation/NOTION_SETUP.md` (personal access token, 2 minutes). Put the token in `NOTION_TOKEN`.

### Telegram bot
1. Talk to @BotFather → `/newbot` → copy the token into `TELEGRAM_BOT_TOKEN`.
2. Get your numeric user id (e.g. from @userinfobot) → `TELEGRAM_ALLOWED_USER_IDS`.

## Development
- `uv run pytest -q`
- `uv run ruff check .`
- Releases & updates: see [RELEASE.md](RELEASE.md).
