from tools import snapshot_json
from tools.sample_workspace import sample_snapshot


def test_round_trip_preserves_every_field(tmp_path):
    snap = sample_snapshot()
    path = tmp_path / "eval" / "workspace.json"  # parent is created on save

    snapshot_json.save(snap, path)
    back = snapshot_json.load(path)

    assert back == snap  # frozen dataclasses: field-by-field, operations and datetimes included


def test_saved_file_keeps_cyrillic_readable(tmp_path):
    path = tmp_path / "workspace.json"
    snapshot_json.save(sample_snapshot(), path)
    assert "Покупки" in path.read_text(encoding="utf-8")
