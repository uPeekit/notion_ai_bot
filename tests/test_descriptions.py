import threading
import time

import pytest
import yaml

from app.notion import descriptions
from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta


def test_load_missing_file_is_empty(tmp_path):
    assert Descriptions(tmp_path / "t.yaml").load() == {}


def test_ensure_adds_and_preserves(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old", description="keep me",
                              fields={"f1": FieldMeta(name="X", description="d", required=True)})})
    merged = d.ensure({"ds1": ("Покупки", {"f1": "Название", "f2": "Магазин"}),
                       "pg1": ("Идеи", {})})
    assert merged["ds1"].name == "Покупки"
    assert merged["ds1"].description == "keep me"
    assert merged["ds1"].fields["f1"].required is True
    assert merged["ds1"].fields["f1"].name == "Название"
    assert merged["ds1"].fields["f2"] == FieldMeta(name="Магазин")
    assert merged["pg1"] == TargetMeta(name="Идеи")
    on_disk = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert on_disk["pg1"]["name"] == "Идеи"
    assert on_disk["ds1"]["fields"]["f1"]["required"] is True


def test_ensure_never_deletes(tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"gone": TargetMeta(name="Gone", description="x")})
    merged = d.ensure({})
    assert merged["gone"].description == "x"


def test_utf8_roundtrip(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"a": TargetMeta(name="Ёжик", description="описание")})
    assert "Ёжик" in p.read_text(encoding="utf-8")
    assert d.load()["a"].description == "описание"


def test_malformed_yaml_syntax_is_tolerated(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("a: [unclosed\n", encoding="utf-8")
    d = Descriptions(p)
    assert d.load() == {}
    assert d.broken is True
    before = p.read_text(encoding="utf-8")
    d.ensure({"ds1": ("Покупки", {})})
    assert p.read_text(encoding="utf-8") == before


def test_top_level_list_is_malformed(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("- a\n- b\n", encoding="utf-8")
    d = Descriptions(p)
    assert d.load() == {}
    assert d.broken is True
    before = p.read_text(encoding="utf-8")
    merged = d.ensure({"ds1": ("Покупки", {})})
    assert merged["ds1"].name == "Покупки"
    assert p.read_text(encoding="utf-8") == before


def test_top_level_scalar_is_malformed(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("just a string\n", encoding="utf-8")
    d = Descriptions(p)
    assert d.load() == {}
    assert d.broken is True
    before = p.read_text(encoding="utf-8")
    d.ensure({"ds1": ("Покупки", {})})
    assert p.read_text(encoding="utf-8") == before


def test_valid_file_still_roundtrips_after_broken_check(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old")})
    assert d.load() == {"ds1": TargetMeta(name="Old")}
    assert d.broken is False
    merged = d.ensure({"ds1": ("Покупки", {})})
    assert merged["ds1"].name == "Покупки"
    assert d.load()["ds1"].name == "Покупки"


def test_inbox_flag_survives_ensure_roundtrip(tmp_path):
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old", inbox=True)})
    merged = d.ensure({"ds1": ("Покупки", {})})
    assert merged["ds1"].inbox is True
    assert d.load()["ds1"].inbox is True


def test_inbox_flag_defaults_false_without_key(tmp_path):
    p = tmp_path / "t.yaml"
    p.write_text("ds1:\n  name: Old\n", encoding="utf-8")
    d = Descriptions(p)
    assert d.load()["ds1"].inbox is False


# ---- the admin HTTP thread and the event loop share this one file ------------------------------


def test_save_replaces_the_file_atomically(tmp_path, monkeypatch):
    """`Path.write_text` truncates first and writes after, so a concurrent reader can see a
    *partial but still valid* yaml document — `ensure()` then re-adds the discovered names to it
    and saves that back, silently losing the user's descriptions and inbox flag. The write must
    land on a temp file and reach its destination through os.replace, which is atomic."""
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old", description="keep me", inbox=True)})
    before = p.read_text(encoding="utf-8")

    def _replace_fails(src, dst):
        raise OSError("replace refused")

    monkeypatch.setattr(descriptions.os, "replace", _replace_fails)
    with pytest.raises(OSError):
        d.save({"ds1": TargetMeta(name="New", description="clobbered")})

    assert p.read_text(encoding="utf-8") == before  # never truncated in place
    assert [f.name for f in tmp_path.iterdir()] == [p.name]  # and no temp file left behind


def test_ensure_and_save_do_not_interleave_across_threads(tmp_path):
    """The real interleaving: the admin thread is inside save() while the event loop is inside
    Discovery._refresh_locked -> ensure(). Both take the same lock, so the read-modify-write pairs
    run one after the other and the user's description survives whichever order they land in."""
    p = tmp_path / "t.yaml"
    d = Descriptions(p)
    d.save({"ds1": TargetMeta(name="Old", description="user wrote this", inbox=True)})

    order: list[str] = []
    real_dump = descriptions.yaml.safe_dump

    def slow_dump(*args, **kwargs):
        order.append("dump-start")
        time.sleep(0.05)
        order.append("dump-end")
        return real_dump(*args, **kwargs)

    descriptions.yaml.safe_dump = slow_dump
    try:
        saver = threading.Thread(
            target=d.save,
            args=({"ds1": TargetMeta(name="Old", description="user wrote this", inbox=True)},),
        )
        saver.start()
        time.sleep(0.01)  # let the saver get inside the critical section first
        merged = d.ensure({"ds1": ("Покупки", {"f1": "Название"})})
        saver.join()
    finally:
        descriptions.yaml.safe_dump = real_dump

    assert order == ["dump-start", "dump-end", "dump-start", "dump-end"]
    assert merged["ds1"].description == "user wrote this"
    assert merged["ds1"].inbox is True
    on_disk = d.load()["ds1"]
    assert on_disk.description == "user wrote this"
    assert on_disk.inbox is True
    assert on_disk.name == "Покупки"
