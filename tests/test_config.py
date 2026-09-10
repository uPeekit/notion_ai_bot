import pytest
from pydantic import ValidationError

from app.config import Settings, load_settings


def test_defaults_load(env):
    s = load_settings()
    assert s.notion_version == "2025-09-03"
    assert s.llm_model == "llama3.1:8b"
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


def test_empty_allowlist_loads_but_require_telegram_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", " ")
    env.delenv("TELEGRAM_BOT_TOKEN")
    s = Settings(_env_file=None)
    assert s.allowed_user_ids == frozenset()
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_IDS"):
        s.require_telegram()
    ok = Settings(_env_file=None, telegram_bot_token="t", telegram_allowed_user_ids="5")
    ok.require_telegram()


def test_bad_user_id_fails(env):
    env.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,abc")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_tokens_are_not_exposed_in_repr_or_str(env):
    s = Settings(_env_file=None)
    assert "ntn-test-token" not in repr(s)
    assert "ntn-test-token" not in str(s)
    assert "tg-test-token" not in repr(s)
    assert "tg-test-token" not in str(s)


def test_items_per_target_out_of_range_fails(env):
    env.setenv("ITEMS_PER_TARGET", "200")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
