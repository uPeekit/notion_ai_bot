"""Builders shared by validation/command/executor tests."""

from __future__ import annotations

from typing import Any

from app.interpretation.models import Interpretation
from app.llm.context import Context, ContextBuilder
from app.notion.snapshot import WorkspaceSnapshot
from tools.sample_workspace import SAMPLE_NOW, sample_snapshot


def ctx_and_snapshot() -> tuple[Context, WorkspaceSnapshot]:
    snap = sample_snapshot()
    return ContextBuilder("Europe/Tallinn").build(snap, now=SAMPLE_NOW), snap


def val(v: Any, conf: float = 0.9, src: str = "") -> dict:
    return {"status": "value", "value": v, "confidence": conf, "source_text": src}


def amb(*cands: Any, src: str = "") -> dict:
    return {"status": "ambiguous", "candidates": list(cands), "source_text": src}


def cand(ctx: Context, target: str, conf: float = 0.9, *, item: str | None = None,
         item_candidates: list[str] | None = None, fields: dict | None = None,
         content: str | None = None, search_query: str | None = None) -> dict:
    base = {k: {"status": "not_mentioned"} for k in ctx.field_keys(target)}
    base.update(fields or {})
    return {"target": target, "confidence": conf, "item": item,
            "item_candidates": item_candidates or [], "fields": base,
            "content": content, "search_query": search_query}


def make_interp(intent: str, *cands: dict, intent_conf: float = 0.95) -> Interpretation:
    return Interpretation.model_validate({
        "intent": {"value": intent, "confidence": intent_conf},
        "candidates": list(cands), "notes": "",
    })
