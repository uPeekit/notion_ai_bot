import tools.discover as discover_mod
from app.notion.errors import NotionError


class FakeSecret:
    def get_secret_value(self) -> str:
        return "tok"


class FakeSettings:
    notion_token = FakeSecret()
    notion_version = "2025-09-03"
    targets_file = "data/targets.yaml"
    items_per_target = 50
    log_level = "DEBUG"


class FakeProvider:
    def __init__(self, *a, **kw) -> None:
        self.me_error: Exception | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def me(self):
        if self.me_error:
            raise self.me_error
        return {"id": "bot"}


def _rig(monkeypatch, error):
    settings = FakeSettings()
    monkeypatch.setattr(discover_mod, "load_settings", lambda: settings)
    provider = FakeProvider()
    provider.me_error = error
    monkeypatch.setattr(discover_mod, "DirectNotionProvider", lambda *a, **kw: provider)
    return settings


async def test_exit_3_only_on_401(monkeypatch):
    _rig(monkeypatch, NotionError(401, "unauthorized", "bad token"))
    assert await discover_mod.main() == 3


async def test_other_notion_error_exits_4(monkeypatch, capsys):
    _rig(monkeypatch, NotionError(400, "validation_error", "bad request"))
    assert await discover_mod.main() == 4
    assert "Notion error: 400 validation_error" in capsys.readouterr().err


async def test_logging_configured_with_settings_log_level(monkeypatch):
    _rig(monkeypatch, NotionError(401, "unauthorized", "bad token"))
    seen = {}
    monkeypatch.setattr(discover_mod.logging, "basicConfig", lambda **kw: seen.update(kw))
    await discover_mod.main()
    assert seen["level"] == "DEBUG"


async def test_reconfigures_stdout_encoding_when_available(monkeypatch):
    calls = []

    class FakeStdout:
        def reconfigure(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(discover_mod.sys, "stdout", FakeStdout())
    _rig(monkeypatch, NotionError(401, "unauthorized", "bad token"))
    await discover_mod.main()
    assert calls == [{"encoding": "utf-8", "errors": "replace"}]


async def test_no_reconfigure_attr_is_tolerated(monkeypatch):
    class FakeStdout:
        pass

    monkeypatch.setattr(discover_mod.sys, "stdout", FakeStdout())
    _rig(monkeypatch, NotionError(401, "unauthorized", "bad token"))
    assert await discover_mod.main() == 3
