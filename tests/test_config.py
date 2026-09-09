import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_defaults_load(env):
    s = load_settings()
    assert s.notion_version == "2025-09-03"
    assert s.llm_model == "qwen3:8b"
    assert s.policy_target_margin == 0.10
    assert s.allowed_user_ids == frozenset({1, 2})
    assert s.timezone == "Europe/Tallinn"


def test_missing_token_fails(env):
    env.delenv("NOTION_TOKEN")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_bad_threshold_fails(env):
    env.setenv("POLICY_TARGET_MIN", "1.5")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_allowlist_parsing(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " 10, 20 ,30 ")
    assert Settings(_env_file=None).allowed_user_ids == frozenset({10, 20, 30})


def test_empty_allowlist_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " ")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
