from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from app.notion.errors import NotionError, NotionUnavailable

log = logging.getLogger(__name__)
BASE_URL = "https://api.notion.com/v1"
MAX_RETRY_AFTER_S = 30.0


class DirectNotionProvider:
    def __init__(
        self,
        token: str,
        version: str = "2025-09-03",
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 30.0,
        max_retries: int = 3,
    ) -> None:
        self._max_retries = max_retries
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": version,
                "Content-Type": "application/json",
            },
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> DirectNotionProvider:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---- low level -------------------------------------------------------

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        attempt_net = 0
        attempt_429 = 0
        attempt_5xx = 0
        while True:
            try:
                resp = await self._client.request(method, path, json=json)
            except httpx.HTTPError as e:
                if method == "POST" or attempt_net >= self._max_retries:
                    raise NotionUnavailable(f"network error: {type(e).__name__}") from None
                attempt_net += 1
                await asyncio.sleep(min(2.0**attempt_net, 8.0))
                continue

            if resp.status_code < 400:
                return resp.json() if resp.content else {}

            if resp.status_code == 429 and attempt_429 < self._max_retries:
                attempt_429 += 1
                raw = resp.headers.get("Retry-After", "1")
                try:
                    delay = min(float(raw), MAX_RETRY_AFTER_S)
                except ValueError:  # HTTP-date form
                    delay = min(2.0**attempt_429, 8.0)
                log.warning("notion 429, retry %d in %.1fs", attempt_429, delay)
                await asyncio.sleep(delay)
                continue

            if resp.status_code >= 500:
                if method != "POST" and attempt_5xx < min(self._max_retries, 2):
                    attempt_5xx += 1
                    await asyncio.sleep(min(2.0**attempt_5xx, 8.0))
                    continue
                raise NotionUnavailable(f"server error {resp.status_code}")

            code, message = self._error_parts(resp)
            raise NotionError(resp.status_code, code, message)

    @staticmethod
    def _error_parts(resp: httpx.Response) -> tuple[str, str]:
        try:
            body: Any = resp.json()
            return str(body.get("code", "error")), str(body.get("message", ""))
        except ValueError:
            return "error", resp.text[:200]

    async def _paginate(self, method: str, path: str, body: dict) -> list[dict]:
        results: list[dict] = []
        cursor: str | None = None
        while True:
            payload = {**body, "page_size": 100}
            if cursor:
                payload["start_cursor"] = cursor
            data = await self._request(method, path, payload)
            results.extend(data.get("results", []))
            if not data.get("has_more"):
                return results
            cursor = data.get("next_cursor")
            if not cursor:
                return results

    # ---- API -------------------------------------------------------------

    async def me(self) -> dict:
        return await self._request("GET", "/users/me")

    async def search(
        self, query: str | None = None, object_type: str | None = None
    ) -> list[dict]:
        body: dict = {}
        if query:
            body["query"] = query
        if object_type:
            body["filter"] = {"property": "object", "value": object_type}
        return await self._paginate("POST", "/search", body)

    async def get_database(self, database_id: str) -> dict:
        return await self._request("GET", f"/databases/{database_id}")

    async def get_data_source(self, data_source_id: str) -> dict:
        return await self._request("GET", f"/data_sources/{data_source_id}")

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 50,
    ) -> list[dict]:
        body: dict = {"page_size": page_size}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts
        data = await self._request("PATCH", f"/data_sources/{data_source_id}/query", body)
        return data.get("results", [])

    async def get_page(self, page_id: str) -> dict:
        return await self._request("GET", f"/pages/{page_id}")

    async def create_page(
        self, parent: dict, properties: dict, children: list[dict] | None = None
    ) -> dict:
        body: dict = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        return await self._request("POST", "/pages", body)

    async def update_page(
        self, page_id: str, *, properties: dict | None = None, archived: bool | None = None
    ) -> dict:
        body: dict = {}
        if properties is not None:
            body["properties"] = properties
        if archived is not None:
            body["archived"] = archived
        return await self._request("PATCH", f"/pages/{page_id}", body)

    async def append_blocks(self, block_id: str, children: list[dict]) -> dict:
        return await self._request(
            "PATCH", f"/blocks/{block_id}/children", {"children": children}
        )

    async def delete_block(self, block_id: str) -> dict:
        return await self._request("DELETE", f"/blocks/{block_id}")
