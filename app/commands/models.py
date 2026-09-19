from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyWrite(Strict):
    property_id: str
    property_name: str
    type: str
    value: Any = None  # None = clear


class CreateItem(Strict):
    action: Literal["create_item"] = "create_item"
    data_source_id: str
    target_name: str
    properties: list[PropertyWrite]
    # The new row's page body (e.g. what a web search found). Lines of Markdown when `markdown`;
    # both default so a command read back from an older audit row still validates.
    body: list[str] = []
    markdown: bool = False


class UpdateItem(Strict):
    action: Literal["update_item"] = "update_item"
    page_id: str
    target_name: str
    item_title: str
    properties: list[PropertyWrite]


class CreatePage(Strict):
    action: Literal["create_page"] = "create_page"
    parent_page_id: str
    target_name: str
    title: str
    body: list[str] = []
    markdown: bool = False  # body is Markdown lines (text the model wrote), not plain paragraphs


class AppendBlocks(Strict):
    action: Literal["append_blocks"] = "append_blocks"
    page_id: str
    target_name: str
    page_title: str
    paragraphs: list[str]
    markdown: bool = False  # paragraphs are Markdown lines; the inbox keeps plain text verbatim
    # The user's own words, used only to choose which list on the page a short line joins. Empty
    # means the page must not be described to the cloud model (a local-only target) or there is
    # nothing to choose from, and the line goes to the end of the page.
    request: str = ""


class Search(Strict):
    action: Literal["search"] = "search"
    data_source_id: str | None
    target_name: str
    title_property: str | None
    query: str
    filters: list[PropertyWrite] = []


Command = CreateItem | UpdateItem | CreatePage | AppendBlocks | Search
