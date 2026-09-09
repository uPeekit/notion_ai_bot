from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

FieldType = Literal[
    "title", "rich_text", "select", "multi_select", "status", "date",
    "checkbox", "number", "url", "relation", "readonly",
]

WRITABLE_TYPES: frozenset[str] = frozenset(
    {"title", "rich_text", "select", "multi_select", "status", "date",
     "checkbox", "number", "url", "relation"}
)

DB_OPERATIONS: frozenset[str] = frozenset({"create", "update", "search"})
PAGE_OPERATIONS: frozenset[str] = frozenset({"create_page", "append", "search"})


@dataclass(frozen=True)
class Option:
    id: str
    name: str


@dataclass(frozen=True)
class Item:
    id: str
    title: str
    hint: str | None
    last_edited: datetime
    url: str


@dataclass(frozen=True)
class Field:
    id: str
    name: str
    type: FieldType
    required: bool
    options: list[Option]
    relation_data_source_id: str | None
    description: str

    @property
    def writable(self) -> bool:
        return self.type in WRITABLE_TYPES


@dataclass(frozen=True)
class Target:
    id: str
    kind: Literal["database", "page"]
    name: str
    path: str
    description: str
    parent_page_id: str | None
    database_id: str | None
    fields: list[Field]
    items: list[Item]
    operations: frozenset[str]
    url: str

    def field(self, field_id: str) -> Field | None:
        return next((f for f in self.fields if f.id == field_id), None)

    def title_field(self) -> Field | None:
        return next((f for f in self.fields if f.type == "title"), None)

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    fetched_at: datetime
    targets: list[Target] = field(default_factory=list)

    def target(self, target_id: str) -> Target | None:
        return next((t for t in self.targets if t.id == target_id), None)

    def databases(self) -> list[Target]:
        return [t for t in self.targets if t.kind == "database"]

    def pages(self) -> list[Target]:
        return [t for t in self.targets if t.kind == "page"]
