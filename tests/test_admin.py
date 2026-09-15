import http.client
import json

import pytest
import yaml

from app.admin.server import MAX_BODY_BYTES, AdminServer
from app.config import Settings
from app.notion.descriptions import Descriptions, TargetMeta
from tools.sample_workspace import sample_snapshot


class FakeDiscovery:
    """Stands in for app.notion.discovery.Discovery: `.last` is a plain attribute, never a
    fetch, and `.invalidate()` just flips a flag so tests can assert it was called."""

    def __init__(self, snapshot=None):
        self._snapshot = snapshot
        self.invalidated = False

    @property
    def last(self):
        return self._snapshot

    def invalidate(self):
        self.invalidated = True


@pytest.fixture
def settings(env):
    return Settings(_env_file=None, admin_ui_port=0)


@pytest.fixture
def descriptions(tmp_path):
    return Descriptions(tmp_path / "targets.yaml")


@pytest.fixture
def discovery():
    return FakeDiscovery(sample_snapshot())


@pytest.fixture
def server(settings, discovery, descriptions):
    s = AdminServer(settings, discovery, descriptions)
    s.start()
    yield s
    s.stop()


def _get(server, path, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.request("GET", path, headers={"Host": host})
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _post(server, path, payload, host="127.0.0.1"):
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    body = payload if isinstance(payload, (bytes, bytearray)) else json.dumps(payload).encode()
    try:
        conn.request(
            "POST", path, body=body,
            headers={"Host": host, "Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_root_serves_page_with_title(server):
    status, body = _get(server, "/")
    assert status == 200
    html = body.decode("utf-8")
    assert "<title>" in html
    assert "Notion" in html


def test_targets_mirrors_snapshot(server):
    status, body = _get(server, "/api/targets")
    assert status == 200
    data = json.loads(body)
    assert "fetched_at" in data
    by_id = {t["id"]: t for t in data["targets"]}
    buy = by_id["ds-buy"]
    assert buy["path"] == "Дом / Покупки"
    assert buy["is_inbox"] is False
    assert buy["description"].startswith("Список покупок")
    title_field = next(f for f in buy["fields"] if f["id"] == "title")
    assert title_field["required"] is True
    shop_field = next(f for f in buy["fields"] if f["id"] == "shop")
    assert shop_field["required"] is False
    assert shop_field["type"] == "select"


def test_targets_empty_without_snapshot(settings, descriptions):
    s = AdminServer(settings, FakeDiscovery(None), descriptions)
    s.start()
    try:
        status, body = _get(s, "/api/targets")
        assert status == 200
        assert json.loads(body)["targets"] == []
    finally:
        s.stop()


def test_post_writes_yaml_and_invalidates_cache(server, discovery, descriptions):
    payload = {
        "targets": {
            "ds-buy": {
                "description": "Мой список покупок",
                "inbox": False,
                "fields": {"shop": {"description": "Сеть магазинов", "required": True}},
            },
        },
    }
    status, body = _post(server, "/api/descriptions", payload)
    assert status == 200
    assert json.loads(body) == {"saved": 1}
    assert discovery.invalidated is True

    on_disk = yaml.safe_load(descriptions._path.read_text(encoding="utf-8"))
    assert on_disk["ds-buy"]["description"] == "Мой список покупок"
    assert on_disk["ds-buy"]["fields"]["shop"]["required"] is True
    assert on_disk["ds-buy"]["fields"]["shop"]["description"] == "Сеть магазинов"


def test_post_preserves_untouched_targets(server, descriptions):
    descriptions.save({"ds-todo": TargetMeta(description="не трогать")})
    payload = {"targets": {"ds-buy": {"description": "x", "inbox": False, "fields": {}}}}
    status, _body = _post(server, "/api/descriptions", payload)
    assert status == 200
    on_disk = descriptions.load()
    assert on_disk["ds-todo"].description == "не трогать"
    assert on_disk["ds-buy"].description == "x"


def test_save_with_no_changes_reports_zero(server, descriptions):
    payload = {"targets": {"ds-buy": {"description": "", "inbox": False, "fields": {}}}}
    status, body = _post(server, "/api/descriptions", payload)
    assert status == 200
    assert json.loads(body) == {"saved": 0}


def test_two_inbox_flags_rejected_and_nothing_written(server, descriptions):
    descriptions.save({"ds-buy": TargetMeta(description="original")})
    before = descriptions._path.read_text(encoding="utf-8")
    payload = {
        "targets": {
            "ds-buy": {"description": "changed", "inbox": True, "fields": {}},
            "ds-todo": {"description": "", "inbox": True, "fields": {}},
        },
    }
    status, _body = _post(server, "/api/descriptions", payload)
    assert status == 400
    assert descriptions._path.read_text(encoding="utf-8") == before


def test_unknown_target_id_rejected(server, descriptions):
    payload = {"targets": {"does-not-exist": {"description": "x", "inbox": False, "fields": {}}}}
    status, _body = _post(server, "/api/descriptions", payload)
    assert status == 400
    assert not descriptions._path.exists()


def test_unknown_field_id_rejected(server, descriptions):
    payload = {
        "targets": {
            "ds-buy": {
                "description": "x", "inbox": False,
                "fields": {"not-a-field": {"description": "y", "required": True}},
            },
        },
    }
    status, _body = _post(server, "/api/descriptions", payload)
    assert status == 400
    assert not descriptions._path.exists()


def test_non_loopback_host_rejected(server):
    status, _body = _get(server, "/api/targets", host="evil.example.com")
    assert status == 403


def test_non_loopback_host_rejected_on_post(server, descriptions):
    payload = {"targets": {}}
    status, _body = _post(server, "/api/descriptions", payload, host="evil.example.com")
    assert status == 403
    assert not descriptions._path.exists()


def test_unknown_path_is_404(server):
    status, _body = _get(server, "/nope")
    assert status == 404


def test_wrong_method_on_known_path_is_404(server):
    status, _body = _post(server, "/api/targets", {"targets": {}})
    assert status == 404


def test_malformed_json_rejected_and_nothing_written(server, descriptions):
    descriptions.save({"ds-buy": TargetMeta(description="original")})
    before = descriptions._path.read_text(encoding="utf-8")
    status, _body = _post(server, "/api/descriptions", b"{not json at all")
    assert status == 400
    assert descriptions._path.read_text(encoding="utf-8") == before


def _post_with_content_length(server, path, body_bytes, content_length_header):
    """Sends a POST with an explicit, possibly-hostile Content-Length header value that need
    not match len(body_bytes) — exercises the server's own defensive parsing of that header,
    bypassing http.client's normal auto-computed Content-Length."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=5)
    try:
        conn.putrequest("POST", path)
        conn.putheader("Host", "127.0.0.1")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", content_length_header)
        conn.endheaders(message_body=body_bytes)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def test_non_numeric_content_length_rejected(server, descriptions):
    status, _body = _post_with_content_length(server, "/api/descriptions", b"{}", "abc")
    assert status == 400
    assert not descriptions._path.exists()


def test_negative_content_length_rejected(server, descriptions):
    status, _body = _post_with_content_length(server, "/api/descriptions", b"{}", "-1")
    assert status == 400
    assert not descriptions._path.exists()


def test_content_length_over_cap_rejected(server, descriptions):
    status, _body = _post_with_content_length(
        server, "/api/descriptions", b"{}", str(MAX_BODY_BYTES + 1)
    )
    assert status == 400
    assert not descriptions._path.exists()
