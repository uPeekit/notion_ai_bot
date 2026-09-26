"""Capture the real Notion workspace, as the bot sees it, for offline benchmarking.

uv run python -m tools.capture_workspace --targets-file C:\\apps\\ai_assistant\\data\\targets.yaml

Read-only against Notion (search, data sources, queries). Descriptions come from the given
targets.yaml, but discovery writes to its descriptions file whenever it finds a new page or
field — so it runs against a temporary *copy*, and the production file is never touched.

The output holds real page and item titles: it goes under data/ (git-ignored) by default.
"""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

from app.config import load_settings
from app.notion.descriptions import Descriptions
from app.notion.direct import DirectNotionProvider
from app.notion.discovery import Discovery
from tools import snapshot_json

DEFAULT_OUT = Path("data/eval/workspace.json")


async def capture(targets_file: Path, out: Path) -> int:
    s = load_settings()
    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "targets.yaml"
        if targets_file.exists():
            shutil.copyfile(targets_file, copy)
        async with DirectNotionProvider(s.notion_token.get_secret_value(), s.notion_version) as p:
            disco = Discovery(p, Descriptions(copy), items_per_target=s.items_per_target)
            snap = await disco.refresh()
    snapshot_json.save(snap, out)
    described = sum(1 for t in snap.targets if t.description)
    print(f"captured {len(snap.targets)} targets ({described} described) -> {out}")
    return 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--targets-file", type=Path, required=True,
                    help="targets.yaml whose descriptions to use (read, never written)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    a = ap.parse_args(argv)
    return asyncio.run(capture(a.targets_file, a.out))


if __name__ == "__main__":
    sys.exit(main())
