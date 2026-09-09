from typing import Protocol


class NotionProvider(Protocol):
    async def me(self) -> dict: ...

    async def search(
        self, query: str | None = None, object_type: str | None = None
    ) -> list[dict]: ...

    async def get_database(self, database_id: str) -> dict: ...

    async def get_data_source(self, data_source_id: str) -> dict: ...

    async def query_data_source(
        self,
        data_source_id: str,
        *,
        filter: dict | None = None,
        sorts: list[dict] | None = None,
        page_size: int = 50,
    ) -> list[dict]: ...

    async def get_page(self, page_id: str) -> dict: ...

    async def create_page(
        self, parent: dict, properties: dict, children: list[dict] | None = None
    ) -> dict: ...

    async def update_page(
        self, page_id: str, *, properties: dict | None = None, archived: bool | None = None
    ) -> dict: ...

    async def append_blocks(self, block_id: str, children: list[dict]) -> dict: ...

    async def delete_block(self, block_id: str) -> dict: ...
