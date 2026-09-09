"""Print what the Notion integration can see, as the bot will see it."""

from __future__ import annotations

import asyncio
import logging
import sys

from app.config import load_settings
from app.notion.descriptions import Descriptions
from app.notion.direct import DirectNotionProvider
from app.notion.discovery import Discovery
from app.notion.errors import NotionError


async def main() -> int:
    logging.basicConfig(level="INFO", format="%(levelname)s %(name)s: %(message)s")
    s = load_settings()
    async with DirectNotionProvider(s.notion_token, s.notion_version) as p:
        try:
            await p.me()
        except NotionError as e:
            print(f"Notion auth failed: {e.status} {e.code}", file=sys.stderr)
            return 3
        disco = Discovery(p, Descriptions(s.targets_file), items_per_target=s.items_per_target)
        snap = await disco.refresh()

    print(f"{len(snap.targets)} targets (descriptions in {s.targets_file}):\n")
    for t in snap.targets:
        kind = "DB " if t.kind == "database" else "PG "
        print(f"{kind}{t.path}   [{t.id}]")
        if t.description:
            print(f"     ~ {t.description}")
        for f in t.fields:
            opts = f" {{{', '.join(o.name for o in f.options[:8])}}}" if f.options else ""
            req = "*" if f.required else ""
            print(f"     - {f.name}{req}: {f.type}{opts}")
        for i in t.items[:10]:
            hint = f" ({i.hint})" if i.hint else ""
            print(f"       • {i.title}{hint}")
        if len(t.items) > 10:
            print(f"       … +{len(t.items) - 10}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
