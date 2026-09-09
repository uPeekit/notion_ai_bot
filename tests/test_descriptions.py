import yaml

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
