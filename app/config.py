from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def Prob(default: float) -> float:
    return Field(default, ge=0.0, le=1.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: SecretStr
    telegram_allowed_user_ids: str
    notion_token: SecretStr
    notion_version: str = "2025-09-03"

    ollama_base_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:8b"
    llm_temperature: float = 0.0
    llm_num_ctx: int = 16384
    llm_timeout_s: float = 120.0

    whisper_model: str = "large-v3-turbo"
    whisper_device: str = "auto"
    whisper_compute_type: str = "int8"
    whisper_language: str = "ru"

    timezone: str = "Europe/Tallinn"
    locale: str = "ru"

    db_path: Path = Path("data/bot.sqlite")
    targets_file: Path = Path("data/targets.yaml")
    schema_cache_ttl_s: int = 60
    items_per_target: int = Field(50, ge=1, le=100)
    admin_ui_port: int = 8787

    policy_intent_min: float = Prob(0.85)
    policy_target_min: float = Prob(0.85)
    policy_target_margin: float = Prob(0.10)
    policy_field_min: float = Prob(0.75)
    policy_date_min: float = Prob(0.80)

    session_ttl_s: int = 900
    undo_window_s: int = 300
    log_level: str = "INFO"

    @field_validator("telegram_allowed_user_ids")
    @classmethod
    def _non_empty_allowlist(cls, v: str) -> str:
        ids = [p.strip() for p in v.split(",") if p.strip()]
        if not ids:
            raise ValueError("TELEGRAM_ALLOWED_USER_IDS must list at least one user id")
        for p in ids:
            if not p.isdigit():
                raise ValueError(f"bad user id: {p!r}")
        return v

    @cached_property
    def allowed_user_ids(self) -> frozenset[int]:
        return frozenset(int(p) for p in self.telegram_allowed_user_ids.split(",") if p.strip())


def load_settings() -> Settings:
    return Settings()
