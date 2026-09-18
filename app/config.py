from functools import cached_property
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def Prob(default: float) -> float:
    return Field(default, ge=0.0, le=1.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr = SecretStr("")
    telegram_allowed_user_ids: str = ""
    notion_token: SecretStr
    notion_version: str = "2025-09-03"

    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "mistral-nemo:12b"
    llm_temperature: float = 0.0
    llm_num_ctx: int = 8192
    llm_timeout_s: float = 120.0
    # How long Ollama keeps the model loaded after a request. Its own default, 5m, means a
    # message after any short pause reloads the model: measured 32 s instead of 16 s. "-1"
    # keeps it loaded for good (always fast, but holds the GPU); "0" unloads right away.
    llm_keep_alive: str = "30m"

    # Claude answers first when a key is set; the local model above is the fallback (offline,
    # rate-limited, out of credit). LLM_CLOUD=false keeps everything on this machine.
    anthropic_api_key: SecretStr = SecretStr("")
    claude_model: str = "claude-haiku-4-5"
    claude_timeout_s: float = 30.0
    llm_cloud: bool = True

    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"

    timezone: str = "Europe/Tallinn"
    locale: str = "ru"

    db_path: Path = Path("data/bot.sqlite")
    targets_file: Path = Path("data/targets.yaml")
    schema_cache_ttl_s: int = 60
    items_per_target: int = Field(15, ge=1, le=100)
    admin_ui_port: int = 8787

    policy_intent_min: float = Prob(0.85)
    policy_target_min: float = Prob(0.85)
    policy_target_margin: float = Prob(0.10)
    policy_field_min: float = Prob(0.75)
    policy_date_min: float = Prob(0.80)

    session_ttl_s: int = 900
    undo_window_s: int = 300
    log_level: str = "INFO"
    log_file: str = "logs/bot.log"  # rotating, redacted like stderr; empty disables it

    # auto: save on every unresolvable path and offer the button; button: only on button press;
    # off: no inbox, no button.
    inbox_mode: Literal["auto", "button", "off"] = "auto"
    # Notion page/data-source id that overrides the targets.yaml `inbox: true` flag.
    inbox_target_id: str = ""

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _allowlist_digits(cls, v: str) -> str:
        for p in (x.strip() for x in v.split(",") if x.strip()):
            if not p.isdigit():
                raise ValueError(f"bad user id: {p!r}")
        return v

    def require_telegram(self) -> None:
        """Raise a clear error when the bot itself is started without Telegram settings."""
        missing = []
        if not self.telegram_bot_token.get_secret_value():
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.allowed_user_ids:
            missing.append("TELEGRAM_ALLOWED_USER_IDS")
        if missing:
            raise ValueError("missing in .env: " + ", ".join(missing))

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.telegram_allowed_user_ids.split(",") if p.strip())


def load_settings() -> Settings:
    return Settings()
