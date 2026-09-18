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
    # Nothing in targets.yaml yet: the value is empty, Notion's own text is only the hint.
    assert buy["description"] == ""
    assert buy["notion_description"].startswith("Список покупок")
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


# ---- the INBOX_TARGET_ID override must not be written into the file ----------------------------


@pytest.fixture
def locked_server(env, discovery, descriptions):
    """A server whose settings carry INBOX_TARGET_ID, i.e. the page's inbox radios are disabled."""
    s = AdminServer(
        Settings(_env_file=None, admin_ui_port=0, inbox_target_id="ds-buy"),
        discovery, descriptions,
    )
    s.start()
    yield s
    s.stop()


def test_inbox_flag_is_not_persisted_while_the_env_override_is_active(
    locked_server, descriptions
):
    """The page disables the radio group when INBOX_TARGET_ID is set, but a disabled radio is
    still `checked` and `collectBody` reads `.checked` — so every save posts the env-chosen
    target's `inbox: true`. Persisting it would leave an inbox the user never picked behind once
    the env var is unset again."""
    payload = {
        "targets": {"ds-buy": {"description": "Мой список", "inbox": True, "fields": {}}},
    }
    status, _ = _post(locked_server, "/api/descriptions", payload)

    assert status == 200
    on_disk = yaml.safe_load(descriptions._path.read_text(encoding="utf-8"))
    assert on_disk["ds-buy"]["description"] == "Мой список"  # the rest of the save still applies
    assert on_disk["ds-buy"]["inbox"] is False


def test_inbox_flag_already_in_the_file_survives_a_locked_save(locked_server, descriptions):
    """The override hides the user's own earlier choice; a save while it is active must not
    quietly clear it either."""
    descriptions.save({"ds-todo": TargetMeta(description="x", inbox=True)})
    payload = {"targets": {"ds-buy": {"description": "y", "inbox": True, "fields": {}}}}

    status, _ = _post(locked_server, "/api/descriptions", payload)

    assert status == 200
    assert descriptions.load()["ds-todo"].inbox is True


def test_inbox_flag_is_persisted_when_no_override_is_set(server, descriptions):
    payload = {"targets": {"ds-buy": {"description": "x", "inbox": True, "fields": {}}}}

    status, _ = _post(server, "/api/descriptions", payload)

    assert status == 200
    assert descriptions.load()["ds-buy"].inbox is True


# ---- the page reports a failed load instead of going blank ------------------------------------


def test_page_surfaces_a_failed_targets_fetch(server):
    """An unhandled promise rejection in load() leaves a blank page and a message only visible in
    the browser console; the page has to say something itself."""
    _, body = _get(server, "/")
    html = body.decode("utf-8")
    load_fn = html.split("function load()", 1)[1].split("function collectBody", 1)[0]
    assert ".catch(" in load_fn
    assert "showLoadError" in load_fn
    assert 'id="load-error"' in html



def test_saved_values_show_immediately_even_with_a_stale_snapshot(server, discovery):
    """The page used to redraw from the snapshot, which discovery refreshes only when a Telegram
    message arrives — so a save looked lost after every reload."""
    stale = discovery.last
    status, _ = _post(server, "/api/descriptions", {"targets": {
        "ds-buy": {"description": "Покупки для дома", "inbox": True,
                   "fields": {"shop": {"description": "Где купить", "required": True}}},
    }})
    assert status == 200
    assert discovery.last is stale  # nothing re-ran discovery

    data = json.loads(_get(server, "/api/targets")[1])
    buy = next(t for t in data["targets"] if t["id"] == "ds-buy")
    assert buy["description"] == "Покупки для дома"
    assert buy["notion_description"] == ""  # a saved value needs no hint
    assert buy["is_inbox"] is True
    shop = next(f for f in buy["fields"] if f["id"] == "shop")
    assert shop["description"] == "Где купить" and shop["required"] is True


def test_saving_what_the_page_shows_changes_nothing(server, descriptions):
    """Save posts every box on the page. If the page showed anything other than the file, a
    save would write that back over it — the stale-snapshot version wrote blanks over real
    descriptions. Load, post it back unchanged, and every saved value must survive."""
    _post(server, "/api/descriptions", {"targets": {
        "ds-buy": {"description": "Покупки для дома", "inbox": True, "fields": {}},
        "ds-todo": {"description": "Дела", "inbox": False,
                    "fields": {"prio": {"description": "Срочность", "required": True}}},
    }})
    shown = json.loads(_get(server, "/api/targets")[1])
    echoed = {"targets": {
        t["id"]: {
            "description": t["description"], "inbox": t["is_inbox"],
            "fields": {f["id"]: {"description": f["description"], "required": f["required"]}
                       for f in t["fields"]},
        }
        for t in shown["targets"]
    }}
    assert _post(server, "/api/descriptions", echoed)[0] == 200

    after = descriptions.load()
    assert after["ds-buy"].description == "Покупки для дома" and after["ds-buy"].inbox
    assert after["ds-todo"].description == "Дела"
    assert after["ds-todo"].fields["prio"].required
    assert after["ds-todo"].fields["prio"].description == "Срочность"


def test_local_only_flag_round_trips_through_the_page(server, descriptions):
    payload = {"targets": {"ds-buy": {"description": "", "inbox": False, "local_only": True,
                                      "fields": {}}}}
    status, body = _post(server, "/api/descriptions", payload)
    assert status == 200 and json.loads(body) == {"saved": 1}
    assert descriptions.load()["ds-buy"].local_only is True
    by_id = {t["id"]: t for t in json.loads(_get(server, "/api/targets")[1])["targets"]}
    assert by_id["ds-buy"]["local_only"] is True and by_id["ds-todo"]["local_only"] is False
