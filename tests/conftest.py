import pytest


@pytest.fixture
def env(monkeypatch):
    """Minimal valid environment for Settings."""
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-test-token")
    monkeypatch.setenv("TELEGRAM_ALLOWED_USER_IDS", "1,2")
    monkeypatch.setenv("NOTION_TOKEN", "ntn-test-token")
    return monkeypatch
