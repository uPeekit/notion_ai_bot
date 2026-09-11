from __future__ import annotations

from app.notion.errors import NotionError


class FakeNotionProvider:
    """In-memory Notion. Feed it search results, data sources, databases, and pages per ds."""

    def __init__(self) -> None:
        self.search_results: list[dict] = []
        self.data_sources: dict[str, dict] = {}
        self.databases: dict[str, dict] = {}
        self.items: dict[str, list[dict]] = {}
        self.pages: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail_search: Exception | None = None

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
        return {"id": "new-page", "url": "https://notion.so/new-page", "properties": properties}

    async def update_page(self, page_id, *, properties=None, archived=None) -> dict:
        self.calls.append(("update_page", page_id, properties, archived))
        return {"id": page_id, "properties": properties or {}, "archived": bool(archived)}

    async def append_blocks(self, block_id, children) -> dict:
        self.calls.append(("append_blocks", block_id, children))
        return {"results": [{"id": f"blk-{i}"} for i, _ in enumerate(children)]}

    async def delete_block(self, block_id) -> dict:
        self.calls.append(("delete_block", block_id))
        return {"id": block_id, "archived": True}
