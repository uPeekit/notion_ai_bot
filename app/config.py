from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Prob = Field(ge=0.0, le=1.0)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str
    telegram_allowed_user_ids: str
    notion_token: str
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
    items_per_target: int = 50
    admin_ui_port: int = 8787

    policy_intent_min: float = Field(0.85, ge=0.0, le=1.0)
    policy_target_min: float = Field(0.85, ge=0.0, le=1.0)
    policy_target_margin: float = Field(0.10, ge=0.0, le=1.0)
    policy_field_min: float = Field(0.75, ge=0.0, le=1.0)
    policy_date_min: float = Field(0.80, ge=0.0, le=1.0)

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
