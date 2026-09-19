"""Images found on the web, re-hosted in Notion.

An external image block only shows while the original site keeps serving the file to Notion —
many block hotlinking, and links rot. So each image is downloaded once, checked to really be an
image, and uploaded through Notion's File Upload API; the block then points at Notion's copy.

The URLs come from a web search, i.e. from arbitrary pages, so the download refuses anything
that is not plain http(s) to a public address: without that, a page could point the bot at
http://127.0.0.1:8787 (the admin page) or at the router on the local network."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from pathlib import PurePosixPath
from urllib.parse import urljoin, urlsplit

import httpx

from app.notion.errors import NotionError
from app.notion.provider import NotionProvider

log = logging.getLogger(__name__)

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3
IMAGE_TYPES = {
    "image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/webp": ".webp",
    "image/svg+xml": ".svg", "image/bmp": ".bmp", "image/tiff": ".tiff", "image/heic": ".heic",
}
_SAFE_NAME = re.compile(r"[^\w.-]+")


class NotAnImage(Exception):
    pass


async def _public(url: str) -> bool:
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return False
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, parts.port or (443 if parts.scheme == "https" else 80),
            type=socket.SOCK_STREAM,
        )
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            return False
    return bool(infos)


def _filename(url: str, content_type: str) -> str:
    stem = PurePosixPath(urlsplit(url).path).stem or "image"
    stem = _SAFE_NAME.sub("_", stem)[:60] or "image"
    return stem + IMAGE_TYPES[content_type]


class ImageHost:
    def __init__(
        self, provider: NotionProvider, *, timeout_s: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._provider = provider
        self._http = httpx.AsyncClient(
            timeout=timeout_s, transport=transport, follow_redirects=False,
            headers={"User-Agent": "Mozilla/5.0 (notion-ai-bot image fetch)"},
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def download(self, url: str) -> tuple[bytes, str]:
        """The image's bytes and content type. Raises NotAnImage for anything else: a private
        or non-http address, an HTML page, a file over MAX_IMAGE_BYTES, a failed request."""
        for _ in range(MAX_REDIRECTS + 1):
            if not await _public(url):
                raise NotAnImage(f"not a public http(s) address: {url}")
            try:
                async with self._http.stream("GET", url) as resp:
                    if resp.is_redirect and "location" in resp.headers:
                        url = urljoin(url, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise NotAnImage(f"HTTP {resp.status_code}")
                    ctype = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                    if ctype not in IMAGE_TYPES:
                        raise NotAnImage(f"content type {ctype or '?'}")
                    data = bytearray()
                    async for chunk in resp.aiter_bytes():
                        data += chunk
                        if len(data) > MAX_IMAGE_BYTES:
                            raise NotAnImage("larger than 5 MB")
                    return bytes(data), ctype
            except httpx.HTTPError as e:
                raise NotAnImage(type(e).__name__) from None
        raise NotAnImage("too many redirects")

    async def host(self, url: str) -> str | None:
        """Notion file-upload id for the image at `url`, or None when it is not one we can
        fetch (the caller then keeps a plain link instead of a broken image)."""
        try:
            data, ctype = await self.download(url)
        except NotAnImage as e:
            log.info("image skipped (%s)", e)
            return None
        try:
            return await self._provider.upload_file(_filename(url, ctype), ctype, data)
        except NotionError as e:
            log.warning("image upload failed (%s); keeping it as a link", e)
            return None
