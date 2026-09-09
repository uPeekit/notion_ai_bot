from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class FieldMeta(BaseModel):
    name: str = ""
    description: str = ""
    required: bool = False


class TargetMeta(BaseModel):
    name: str = ""
    description: str = ""
    fields: dict[str, FieldMeta] = Field(default_factory=dict)


class Descriptions:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[str, TargetMeta]:
        if not self._path.exists():
            return {}
        raw = yaml.safe_load(self._path.read_text(encoding="utf-8")) or {}
        return {str(k): TargetMeta.model_validate(v or {}) for k, v in raw.items()}

    def save(self, meta: dict[str, TargetMeta]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = {k: v.model_dump() for k, v in meta.items()}
        self._path.write_text(
            yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    def ensure(self, discovered: dict[str, tuple[str, dict[str, str]]]) -> dict[str, TargetMeta]:
        meta = self.load()
        before = {k: v.model_dump() for k, v in meta.items()}
        for tid, (name, fields) in discovered.items():
            t = meta.setdefault(tid, TargetMeta())
            t.name = name
            for fid, fname in fields.items():
                f = t.fields.setdefault(fid, FieldMeta())
                f.name = fname
        if {k: v.model_dump() for k, v in meta.items()} != before:
            self.save(meta)
        return meta
