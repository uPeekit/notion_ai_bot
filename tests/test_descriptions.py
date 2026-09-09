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
