"""WorkspaceSnapshot <-> JSON, so a real workspace can be captured once and replayed offline.

Used by tools/capture_workspace.py (write) and tools/benchmark_llm.py --workspace (read). A
captured snapshot holds real page and item titles: keep it under data/, which git ignores.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from app.notion.snapshot import Field, Item, Option, Target, WorkspaceSnapshot


def to_dict(snap: WorkspaceSnapshot) -> dict[str, Any]:
    def target(t: Target) -> dict[str, Any]:
        d = asdict(t)
        d["operations"] = sorted(t.operations)
        for item in d["items"]:
            item["last_edited"] = item["last_edited"].isoformat()
        return d

    return {"fetched_at": snap.fetched_at.isoformat(), "targets": [target(t) for t in snap.targets]}


def from_dict(d: dict[str, Any]) -> WorkspaceSnapshot:
    def field(f: dict[str, Any]) -> Field:
        return Field(**{**f, "options": [Option(**o) for o in f["options"]]})

    def item(i: dict[str, Any]) -> Item:
        return Item(**{**i, "last_edited": datetime.fromisoformat(i["last_edited"])})

    def target(t: dict[str, Any]) -> Target:
        return Target(**{
            **t,
            "fields": [field(f) for f in t["fields"]],
            "items": [item(i) for i in t["items"]],
            "operations": frozenset(t["operations"]),
        })

    return WorkspaceSnapshot(
        fetched_at=datetime.fromisoformat(d["fetched_at"]),
        targets=[target(t) for t in d["targets"]],
    )


def save(snap: WorkspaceSnapshot, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(to_dict(snap), ensure_ascii=False, indent=1), encoding="utf-8")


def load(path: Path) -> WorkspaceSnapshot:
    return from_dict(json.loads(path.read_text(encoding="utf-8")))
