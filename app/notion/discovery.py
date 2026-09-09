from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.notion import props
from app.notion.descriptions import Descriptions, TargetMeta
from app.notion.errors import NotionError
from app.notion.provider import NotionProvider
from app.notion.snapshot import (
    DB_OPERATIONS,
    PAGE_OPERATIONS,
    Field,
    Item,
    Option,
    Target,
    WorkspaceSnapshot,
)

log = logging.getLogger(__name__)
STALE_MAX = timedelta(hours=1)
CONCURRENCY = 3


def now_utc() -> datetime:
    return datetime.now(UTC)


def _parse_time(s: str | None) -> datetime:
    if not s:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class Discovery:
    def __init__(
        self,
        provider: NotionProvider,
        descriptions: Descriptions,
        *,
        items_per_target: int = 50,
        ttl_s: int = 60,
        clock: Callable[[], datetime] = now_utc,
    ) -> None:
        self._p = provider
        self._desc = descriptions
        self._items_per_target = items_per_target
        self._ttl = timedelta(seconds=ttl_s)
        self._clock = clock
        self._last: WorkspaceSnapshot | None = None
        self._lock = asyncio.Lock()

    @property
    def last(self) -> WorkspaceSnapshot | None:
        return self._last

    def invalidate(self) -> None:
        self._last = None

    async def get(self) -> WorkspaceSnapshot:
        async with self._lock:
            now = self._clock()
            if self._last and now - self._last.fetched_at < self._ttl:
                return self._last
            try:
                return await self._refresh_locked()
            except NotionError as e:
                if self._last and now - self._last.fetched_at < STALE_MAX:
                    log.warning("discovery failed (%s); using stale snapshot", e.code)
                    return self._last
                raise

    async def refresh(self) -> WorkspaceSnapshot:
        async with self._lock:
            return await self._refresh_locked()

    # ---- internals -------------------------------------------------------

    async def _refresh_locked(self) -> WorkspaceSnapshot:
        results = await self._p.search()
        pages = [r for r in results if r.get("object") == "page"]
        ds_ids = [r["id"] for r in results if r.get("object") == "data_source"]

        sem = asyncio.Semaphore(CONCURRENCY)

        async def fetch_ds(ds_id: str) -> tuple[dict, list[dict]]:
            async with sem:
                ds = await self._p.get_data_source(ds_id)
                items = await self._p.query_data_source(
                    ds_id,
                    sorts=[{"timestamp": "last_edited_time", "direction": "descending"}],
                    page_size=self._items_per_target,
                )
            return ds, items

        fetched = await asyncio.gather(*(fetch_ds(i) for i in ds_ids))
        sources = {ds["id"]: ds for ds, _ in fetched}
        items_by_ds = {ds["id"]: rows for ds, rows in fetched}

        db_ids = {ds.get("parent", {}).get("database_id") for ds in sources.values()}
        db_ids.discard(None)

        async def fetch_db(db_id: str) -> tuple[str, dict]:
            async with sem:
                try:
                    return db_id, await self._p.get_database(db_id)
                except NotionError as e:
                    log.warning("database %s not readable: %s", db_id, e.code)
                    return db_id, {}

        databases = dict(await asyncio.gather(*(fetch_db(i) for i in db_ids)))

        # relation targets not visible via search: try one query, else empty
        for ds in sources.values():
            for prop in ds.get("properties", {}).values():
                rel = (
                    prop.get("relation", {}).get("data_source_id")
                    if prop.get("type") == "relation"
                    else None
                )
                if rel and rel not in items_by_ds:
                    try:
                        items_by_ds[rel] = await self._p.query_data_source(
                            rel, page_size=self._items_per_target
                        )
                    except NotionError:
                        items_by_ds[rel] = []

        top_pages = [
            p for p in pages if p.get("parent", {}).get("type") in ("workspace", "page_id")
        ]
        page_by_id = {p["id"]: p for p in top_pages}
        titles = {pid: props.page_title(p) for pid, p in page_by_id.items()}
        for db_id, _db in databases.items():
            titles[db_id] = ""  # databases are transparent in paths
        for ds in sources.values():
            titles[ds["id"]] = props.plain_text(ds.get("title", []))

        def parent_page_of(parent: dict) -> str | None:
            t = parent.get("type")
            if t == "page_id":
                return parent.get("page_id")
            if t == "database_id":
                db = databases.get(parent["database_id"], {})
                return parent_page_of(db.get("parent", {})) if db else None
            return None

        def path_of(name: str, parent_page_id: str | None) -> str:
            chain = [name]
            seen = set()
            pid = parent_page_id
            while pid and pid in page_by_id and pid not in seen:
                seen.add(pid)
                chain.append(titles[pid])
                pid = parent_page_of(page_by_id[pid].get("parent", {}))
            return " / ".join(reversed(chain))

        discovered: dict[str, tuple[str, dict[str, str]]] = {}
        targets: list[Target] = []

        for ds_id, ds in sources.items():
            name = titles[ds_id] or props.UNTITLED
            db_id = ds.get("parent", {}).get("database_id")
            parent_page = (
                parent_page_of(databases.get(db_id, {}).get("parent", {})) if db_id else None
            )
            field_names: dict[str, str] = {}
            fields: list[Field] = []
            for pname, prop in ds.get("properties", {}).items():
                ftype = props.field_type(prop)
                options: list[Option] = []
                rel_ds = None
                if ftype in ("select", "multi_select", "status"):
                    options = [
                        Option(o["id"], o["name"]) for o in prop.get(ftype, {}).get("options", [])
                    ]
                elif ftype == "relation":
                    rel_ds = prop.get("relation", {}).get("data_source_id")
                    options = [
                        Option(r["id"], props.page_title(r)) for r in items_by_ds.get(rel_ds, [])
                    ]
                fields.append(
                    Field(
                        id=prop["id"], name=pname, type=ftype, required=ftype == "title",
                        options=options, relation_data_source_id=rel_ds, description="",
                    )
                )
                field_names[prop["id"]] = pname
            items = [
                Item(id=r["id"], title=props.page_title(r), hint=props.item_hint(r),
                     last_edited=_parse_time(r.get("last_edited_time")))
                for r in items_by_ds.get(ds_id, [])
            ]
            discovered[ds_id] = (name, field_names)
            targets.append(Target(
                id=ds_id, kind="database", name=name, path=path_of(name, parent_page),
                description=props.plain_text(ds.get("description", [])),
                parent_page_id=parent_page, database_id=db_id, fields=fields, items=items,
                operations=DB_OPERATIONS, url=ds.get("url", ""),
            ))

        children: dict[str, list[dict]] = {}
        for p in top_pages:
            pp = parent_page_of(p.get("parent", {}))
            if pp:
                children.setdefault(pp, []).append(p)

        for pid, p in page_by_id.items():
            name = titles[pid]
            parent_page = parent_page_of(p.get("parent", {}))
            kids = sorted(
                children.get(pid, []), key=lambda c: c.get("last_edited_time", ""), reverse=True
            )
            items = [Item(id=c["id"], title=titles[c["id"]], hint=None,
                          last_edited=_parse_time(c.get("last_edited_time")))
                     for c in kids[: self._items_per_target]]
            discovered[pid] = (name, {})
            targets.append(Target(
                id=pid, kind="page", name=name, path=path_of(name, parent_page), description="",
                parent_page_id=parent_page, database_id=None, fields=[], items=items,
                operations=PAGE_OPERATIONS, url=p.get("url", ""),
            ))

        meta = self._desc.ensure(discovered)
        targets = [self._apply_meta(t, meta.get(t.id)) for t in targets]
        targets.sort(key=lambda t: t.path)
        self._last = WorkspaceSnapshot(fetched_at=self._clock(), targets=targets)
        log.info("discovered %d targets", len(targets))
        return self._last

    @staticmethod
    def _apply_meta(t: Target, m: TargetMeta | None) -> Target:
        if m is None:
            return t
        fields = []
        for f in t.fields:
            fm = m.fields.get(f.id)
            if fm is None:
                fields.append(f)
                continue
            fields.append(Field(
                id=f.id, name=f.name, type=f.type,
                required=f.required or fm.required,
                options=f.options, relation_data_source_id=f.relation_data_source_id,
                description=fm.description,
            ))
        return Target(
            id=t.id, kind=t.kind, name=t.name, path=t.path,
            description=m.description or t.description,
            parent_page_id=t.parent_page_id, database_id=t.database_id, fields=fields,
            items=t.items, operations=t.operations, url=t.url,
        )
