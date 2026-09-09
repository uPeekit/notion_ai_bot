import pytest


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Minimal valid environment for Settings, isolated from a developer's real .env."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
    monkeypatch.setenv("NOTION_TOKEN", "ntn-test-token")
    return monkeypatch
