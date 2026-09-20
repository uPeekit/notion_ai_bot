from __future__ import annotations

import copy

from app.interpretation.models import Interpretation
from app.llm.base import LLMTrace
from app.llm.context import Context
from app.notion.errors import NotionError
from app.notion.snapshot import WorkspaceSnapshot


class FakeNotionProvider:
    """In-memory Notion. Feed it search results, data sources, databases, and pages per ds."""

    def __init__(self) -> None:
        self.search_results: list[dict] = []
        self.data_sources: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.pages: dict[str, dict] = {}
        self.page_blocks: dict[str, list[dict]] = {}  # a page's own contents, for _match_list
        self.calls: list[tuple] = []
        self.fail_search: Exception | None = None
        self.fail_create_page: Exception | None = None
        self.fail_append_blocks: Exception | None = None

    async def me(self) -> dict:
        return {"object": "user", "id": "bot"}

    async def search(self, query=None, object_type=None) -> list[dict]:
        self.calls.append(("search", query, object_type))
        if self.fail_search:
            raise self.fail_search
        res = self.search_results
        if object_type:
            res = [r for r in res if r["object"] == object_type]
        return list(res)

    async def get_database(self, database_id: str) -> dict:
        self.calls.append(("get_database", database_id))
        if database_id not in self.databases:
            raise NotionError(404, "object_not_found", database_id)
        return self.databases[database_id]

    async def get_data_source(self, data_source_id: str) -> dict:
        self.calls.append(("get_data_source", data_source_id))
        if data_source_id not in self.data_sources:
            raise NotionError(404, "object_not_found", data_source_id)
        return self.data_sources[data_source_id]

    async def query_data_source(self, data_source_id, *, filter=None, sorts=None, page_size=50):
        self.calls.append(("query", data_source_id, page_size))
        if data_source_id not in self.data_sources:
            raise NotionError(404, "object_not_found", data_source_id)
        rows = list(self.items.get(data_source_id, []))
        if sorts and any(
            s.get("timestamp") == "last_edited_time" and s.get("direction") == "descending"
            for s in sorts
        ):
            rows.sort(key=lambda r: r.get("last_edited_time", ""), reverse=True)
        return rows[:page_size]

    async def get_page(self, page_id: str) -> dict:
        self.calls.append(("get_page", page_id))
        return self.pages.get(page_id, {"id": page_id, "properties": {}})

    async def create_page(self, parent, properties, children=None) -> dict:
        self.calls.append(("create_page", parent, properties, children))
        if self.fail_create_page:
            raise self.fail_create_page
        return {"id": "new-page", "url": "https://notion.so/new-page", "properties": properties}

    async def update_page(self, page_id, *, properties=None, archived=None) -> dict:
        self.calls.append(("update_page", page_id, properties, archived))
        return {"id": page_id, "properties": properties or {}, "archived": bool(archived)}

    async def block_children(self, block_id, limit: int = 100) -> list[dict]:
        self.calls.append(("block_children", block_id))
        return list(self.page_blocks.get(block_id, []))

    async def append_blocks(self, block_id, children, after=None) -> dict:
        self.calls.append(("append_blocks", block_id, children, after))
        if self.fail_append_blocks:
            raise self.fail_append_blocks
        return {"results": [{"id": f"blk-{i}"} for i, _ in enumerate(children)]}

    async def delete_block(self, block_id) -> dict:
        self.calls.append(("delete_block", block_id))
        return {"id": block_id, "archived": True}

    async def upload_file(self, filename, content_type, data) -> str:
        self.calls.append(("upload_file", filename, content_type, len(data)))
        return f"upload-{len(self.calls)}"


class FakeLLM:
    """Serves queued interpretations (or raises queued LLMErrors) instead of calling Ollama, and
    records what it was asked: the prompt text, a deep copy of the context payload it was handed
    (so a later request cannot mutate what an earlier assertion inspects) and the JSON schema.
    `calls` is the assertion hook for "this path must not touch the LLM" — button answers."""

    def __init__(self) -> None:
        self.model = "fake-model"
        self.calls = 0
        self.seen: list[tuple[str, dict, dict]] = []
        self._queue: list[Interpretation | Exception] = []

    def queue(self, interp: Interpretation) -> None:
        self._queue.append(interp)

    def queue_error(self, exc: Exception) -> None:
        self._queue.append(exc)

    async def interpret(
        self, text: str, context: Context, schema: dict
    ) -> tuple[Interpretation, LLMTrace]:
        self.calls += 1
        self.seen.append((text, copy.deepcopy(context.payload), schema))
        if not self._queue:
            raise AssertionError("FakeLLM called with nothing queued")
        item = self._queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item, LLMTrace(
            model=self.model, messages=[{"role": "user", "content": text}],
            raw_response=item.model_dump_json(), duration_ms=7, attempts=1,
            prompt_tokens=123, output_tokens=45, done_reason="stop",
        )

    async def models(self) -> list[str]:
        return [self.model]


class FakeDiscovery:
    """Serves one fixed WorkspaceSnapshot, or raises `fail`. Discovery's own caching, TTL and
    stale-snapshot fallback are covered by test_discovery.py; what the orchestrator needs from
    it is `await get()` plus `last` (the snapshot the inbox fallback writes against)."""

    def __init__(self, snapshot: WorkspaceSnapshot) -> None:
        self.snapshot = snapshot
        self.fail: Exception | None = None
        self.last: WorkspaceSnapshot | None = None

    async def get(self) -> WorkspaceSnapshot:
        if self.fail is not None:
            raise self.fail
        self.last = self.snapshot
        return self.snapshot

    def invalidate(self) -> None:
        self.invalidations = getattr(self, "invalidations", 0) + 1

    def note_new_item(self, target_id: str, item_id: str, title: str, url: str = "") -> None:
        """Real Discovery patches its cached snapshot; here the calls are just recorded, and
        the snapshot this serves is fixed anyway."""
        self.noted = [*getattr(self, "noted", []), (target_id, item_id, title)]
