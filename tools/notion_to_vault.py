"""Migrate the Notion workspace into an Obsidian vault (read-only against Notion).

  uv run python -m tools.notion_to_vault --vault C:\\data\\obsidian \\
      --config data\\vault_migration.yaml [--write]

Without --write it is a dry run: it reads Notion and prints every file it would create, and
writes nothing. With --write it writes the vault; re-running refreshes only the files it wrote
itself and that are still as it left them (see app/vault/notion_import.apply).

The Notion token comes from the .env in the current directory (as the bot reads it) and is
never printed. The config holds real page names, so it lives under data/ (git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from pathlib import Path, PurePosixPath

import httpx

from app.config import load_settings
from app.notion.direct import DirectNotionProvider
from app.notion.images import USER_AGENT, is_public_url
from app.vault.notion_import import ImportConfig, NotionImporter, apply

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_REDIRECTS = 3


class Downloader:
    """Files a page links to: Notion's own (signed, expiring URLs) and external ones. Only
    public http(s) addresses, as for images the bot re-hosts."""

    def __init__(self) -> None:
        self._http = httpx.AsyncClient(timeout=60.0, follow_redirects=False,
                                       headers={"User-Agent": USER_AGENT})

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __call__(self, url: str) -> bytes:
        for _ in range(MAX_REDIRECTS + 1):
            if not await is_public_url(url):
                raise ValueError("not a public http(s) address")
            async with self._http.stream("GET", url) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    url = str(resp.url.join(resp.headers["location"]))
                    continue
                resp.raise_for_status()
                data = bytearray()
                async for chunk in resp.aiter_bytes():
                    data += chunk
                    if len(data) > MAX_FILE_BYTES:
                        raise ValueError("larger than 50 MB")
                return bytes(data)
        raise ValueError("too many redirects")


def _summary(files: list[str]) -> str:
    top = Counter(PurePosixPath(f).parts[0] if len(PurePosixPath(f).parts) > 1 else "/"
                  for f in files)
    return ", ".join(f"{k}: {v}" for k, v in sorted(top.items()))


async def run(vault: Path, config: Path, write: bool) -> int:
    s = load_settings()
    cfg = ImportConfig.load(config)
    async with DirectNotionProvider(s.notion_token.get_secret_value(), s.notion_version) as p:
        plan = await NotionImporter(p, cfg).plan()
        print(f"{len(plan.files)} notes, {len(plan.downloads)} files to download "
              f"({_summary([*plan.files, *plan.downloads])})")
        for line in plan.report:
            print(" -", line)
        if not write:
            for path in sorted(plan.files):
                print("  ", path)
            print("dry run: nothing written (add --write)")
            return 0
        # Notion's file links expire an hour after they were read, so the download follows
        # the plan straight away.
        download = Downloader()
        try:
            report = await apply(plan, vault, download)
        finally:
            await download.aclose()
    print(f"written {len(report.written)}, unchanged {len(report.unchanged)}")
    for title, items in (("kept (edited by you)", report.kept_edited),
                         ("kept (not the import's file)", report.kept_foreign),
                         ("download failed", report.failed)):
        if items:
            print(f"{title}:")
            for item in items:
                print("  ", item)
    return 1 if report.failed else 0


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--vault", type=Path, required=True)
    ap.add_argument("--config", type=Path, required=True)
    ap.add_argument("--write", action="store_true", help="write the vault (default: dry run)")
    a = ap.parse_args(argv)
    return asyncio.run(run(a.vault, a.config, a.write))


if __name__ == "__main__":
    sys.exit(main())
