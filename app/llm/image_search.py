"""Where pictures for a web-research note come from: real addresses, never ones a model wrote.

Asked for image links, a model invents plausible ones (a Wikimedia path with the wrong hash
directory: every one a 404). So the model only proposes search phrases, and the links come from
two places that return real files:

- Wikimedia Commons' search API — free, keyless, and the files are downloadable;
- the pages the text research cited: a page names its own main picture in `og:image`.

Every link still goes through ImageHost.is_image before it is kept."""

from __future__ import annotations

import logging
import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urljoin

import httpx

from app.notion.images import USER_AGENT, is_public_url

log = logging.getLogger(__name__)

COMMONS_API = "https://commons.wikimedia.org/w/api.php"
THUMB_WIDTH = 960  # one of Wikimedia's standard thumbnail widths
MAX_HTML_BYTES = 512 * 1024  # og:image sits in <head>
_OG_IMAGE = re.compile(
    r"""<meta[^>]+(?:property|name)=["'](?:og:image|twitter:image)["'][^>]*>""", re.I)
_CONTENT = re.compile(r"""content=["']([^"']+)["']""", re.I)
_TITLE = re.compile(r"<title[^>]*>([^<]{1,200})</title>", re.I)


def _caption(file_title: str) -> str:
    """'File:Itsukushima_Gate.jpg' -> 'Itsukushima Gate'."""
    name = file_title.split(":", 1)[-1]
    return PurePosixPath(name).stem.replace("_", " ").strip()


class ImageSearch:
    def __init__(
        self, *, timeout_s: float = 15.0, transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._http = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, follow_redirects=False,
            headers={"User-Agent": USER_AGENT},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def commons(self, phrase: str, limit: int = 6) -> list[tuple[str, str]]:
        """(image url, caption) for Commons files matching `phrase`; [] on any failure."""
        try:
            resp = await self._http.get(COMMONS_API, params={
                "action": "query", "format": "json", "generator": "search",
                "gsrnamespace": 6, "gsrsearch": f"{phrase} filetype:bitmap",
                "gsrlimit": limit, "prop": "imageinfo", "iiprop": "url",
                "iiurlwidth": THUMB_WIDTH,
            })
            pages = resp.json().get("query", {}).get("pages", {}) if resp.is_success else {}
        except (httpx.HTTPError, ValueError) as e:
            log.info("commons search failed (%s)", type(e).__name__)
            return []
        found = []
        for page in sorted(pages.values(), key=lambda p: p.get("index", 0)):
            info = (page.get("imageinfo") or [{}])[0]
            url = info.get("thumburl") or info.get("url")
            if url:
                found.append((url, _caption(page.get("title", ""))))
        return found

    async def og_image(self, page_url: str) -> tuple[str, str] | None:
        """The picture a page names as its own (og:image), with the page title as caption."""
        if not await is_public_url(page_url):
            return None
        try:
            async with self._http.stream("GET", page_url) as resp:
                if resp.status_code != 200 or "html" not in resp.headers.get("content-type", ""):
                    return None
                html = bytearray()
                async for chunk in resp.aiter_bytes():
                    html += chunk
                    if len(html) > MAX_HTML_BYTES:
                        break
        except httpx.HTTPError:
            return None
        text = html.decode("utf-8", errors="replace")
        meta = _OG_IMAGE.search(text)
        content = _CONTENT.search(meta.group(0)) if meta else None
        if content is None:
            return None
        title = _TITLE.search(text)
        caption = unquote(title.group(1)).strip() if title else ""
        return urljoin(page_url, content.group(1).replace("&amp;", "&")), caption
