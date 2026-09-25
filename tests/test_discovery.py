import logging
from datetime import UTC, datetime, timedelta

import pytest

from app.notion.descriptions import Descriptions, FieldMeta, TargetMeta
from app.notion.discovery import Discovery
from app.notion.errors import NotionError, NotionUnavailable
from tests.fakes import FakeNotionProvider


def rich(text):
    return [{"plain_text": text}]


def page(id, title, parent, edited="2026-09-01T00:00:00.000Z", extra=None):
    props = {"title": {"id": "title", "type": "title", "title": rich(title)}}
    props.update(extra or {})
    return {"object": "page", "id": id, "url": f"https://notion.so/{id}",
            "last_edited_time": edited, "parent": parent, "properties": props}


@pytest.fixture
def fake():
    f = FakeNotionProvider()
    f.search_results = [
        page("home", "Дом", {"type": "workspace", "workspace": True}),
        page("ideas", "Идеи", {"type": "page_id", "page_id": "home"}),
        page("trip", "Отпуск", {"type": "page_id", "page_id": "ideas"}),
        {"object": "data_source", "id": "ds-buy",
         "parent": {"type": "database_id", "database_id": "db-buy"}},
        {"object": "data_source", "id": "ds-shops",
         "parent": {"type": "database_id", "database_id": "db-shops"}},
        page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
    ]
    f.databases = {
        "db-buy": {"id": "db-buy", "parent": {"type": "page_id", "page_id": "home"}},
        "db-shops": {"id": "db-shops", "parent": {"type": "workspace", "workspace": True}},
    }
    f.data_sources = {
        "ds-buy": {
            "id": "ds-buy", "title": rich("Покупки"), "description": rich("Из Notion"),
            "url": "https://notion.so/ds-buy", "parent": {"database_id": "db-buy"},
            "properties": {
                "Название": {"id": "title", "type": "title"},
                "Магазин": {"id": "shop", "type": "select",
                            "select": {"options": [{"id": "o1", "name": "Rimi"},
                                                   {"id": "o2", "name": "Prisma"}]}},
                "Куплено": {"id": "done", "type": "checkbox"},
                "Где": {"id": "rel", "type": "relation",
                        "relation": {"data_source_id": "ds-shops"}},
                "Формула": {"id": "fx", "type": "formula"},
            },
        },
        "ds-shops": {
            "id": "ds-shops", "title": rich("Магазины"), "description": [],
            "url": "https://notion.so/ds-shops", "parent": {"database_id": "db-shops"},
            "properties": {"Name": {"id": "title", "type": "title"}},
        },
    }
    f.items = {
        "ds-buy": [
            page("row1", "Хлеб", {"type": "data_source_id", "data_source_id": "ds-buy"}),
            page("row2", "Молоко", {"type": "data_source_id", "data_source_id": "ds-buy"},
                 edited="2026-09-02T00:00:00.000Z",
                 extra={"Куплено": {"type": "checkbox", "checkbox": True}}),
        ],
        "ds-shops": [page("s1", "Rimi Hyper", {"type": "data_source_id",
                                               "data_source_id": "ds-shops"})],
    }
    return f


@pytest.fixture
def disco(fake, tmp_path):
    return Discovery(fake, Descriptions(tmp_path / "t.yaml"), items_per_target=10, ttl_s=60)


async def test_databases_discovered(disco):
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.kind == "database"
    assert buy.name == "Покупки"
    assert buy.path == "Дом / Покупки"
    assert buy.database_id == "db-buy"
    assert buy.parent_page_id == "home"
    assert buy.description == "Из Notion"
    assert {f.id: f.type for f in buy.fields} == {"title": "title", "shop": "select",
                                                  "done": "checkbox", "rel": "relation",
                                                  "fx": "readonly"}
    assert buy.field("title").required is True
    assert [o.name for o in buy.field("shop").options] == ["Rimi", "Prisma"]
    assert buy.field("rel").relation_data_source_id == "ds-shops"
    assert [o.name for o in buy.field("rel").options] == ["Rimi Hyper"]
    assert [(i.id, i.title, i.hint) for i in buy.items] == [("row2", "Молоко", "Куплено"),
                                                            ("row1", "Хлеб", None)]
    assert buy.items[0].url == "https://notion.so/row2"
    assert buy.operations == frozenset({"create", "update", "search"})
    assert snap.target("ds-shops").path == "Магазины"


async def test_pages_discovered_with_children(disco):
    snap = await disco.refresh()
    ideas = snap.target("ideas")
    assert ideas.kind == "page"
    assert ideas.path == "Дом / Идеи"
    assert ideas.parent_page_id == "home"
    assert [i.title for i in ideas.items] == ["Отпуск"]
    assert ideas.operations == frozenset({"create_page", "append", "rewrite", "search"})
    assert snap.target("row1") is None  # rows are not page targets
    assert snap.target("home").path == "Дом"


async def test_descriptions_override_and_required(disco, tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(description="Мой список",
                                 fields={"shop": FieldMeta(description="Сеть", required=True)})})
    snap = await disco.refresh()
    buy = snap.target("ds-buy")
    assert buy.description == "Мой список"
    assert buy.field("shop").required is True
    assert buy.field("shop").description == "Сеть"
    on_disk = d.load()
    assert on_disk["ideas"].name == "Идеи"
    assert on_disk["ds-buy"].fields["done"].name == "Куплено"


async def test_cache_ttl_and_invalidate(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    b = await disco.get()
    assert a is b

    # Past the TTL the caller still gets the snapshot it came for — re-reading the workspace
    # costs about fifteen Notion requests, and nobody waits for them any more.
    t["now"] += timedelta(seconds=61)
    assert (await disco.get()) is a
    await disco.settled()
    c = await disco.get()
    assert c is not a

    # An explicit invalidate is a different matter: that one waits.
    disco.invalidate()
    assert (await disco.get()) is not c
    assert sum(1 for c in fake.calls if c[0] == "search") == 3


async def test_stale_snapshot_on_failure(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    fake.fail_search = NotionUnavailable()
    t["now"] += timedelta(minutes=30)
    assert (await disco.get()) is a
    t["now"] += timedelta(minutes=31)
    with pytest.raises(NotionUnavailable):
        await disco.get()


async def test_invalidate_keeps_stale_snapshot_on_failure(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    a = await disco.get()
    disco.invalidate()
    fake.fail_search = NotionUnavailable()
    b = await disco.get()
    assert b is a
    fake.fail_search = None
    c = await disco.get()
    assert c is not a


async def test_missing_relation_target_gives_empty_options(disco, fake):
    del fake.data_sources["ds-shops"]
    fake.search_results = [r for r in fake.search_results if r["id"] != "ds-shops"]
    snap = await disco.refresh()
    assert snap.target("ds-buy").field("rel").options == []


async def test_inbox_flag_from_yaml_sets_is_inbox(fake, tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(inbox=True)})
    disco = Discovery(fake, d, items_per_target=10, ttl_s=60)
    snap = await disco.refresh()
    assert snap.target("ds-buy").is_inbox is True
    assert snap.target("ds-shops").is_inbox is False


async def test_inbox_target_id_override_wins_over_yaml_flag(fake, tmp_path):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(inbox=True)})
    disco = Discovery(fake, d, items_per_target=10, ttl_s=60, inbox_target_id="ds-shops")
    snap = await disco.refresh()
    assert snap.target("ds-shops").is_inbox is True
    assert snap.target("ds-buy").is_inbox is False


async def test_inbox_target_id_matching_nothing_disables_inbox_with_warning(
    fake, tmp_path, caplog
):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(inbox=True)})
    disco = Discovery(fake, d, items_per_target=10, ttl_s=60, inbox_target_id="no-such-id")
    with caplog.at_level(logging.WARNING):
        snap = await disco.refresh()
    assert snap.target("ds-buy").is_inbox is False
    assert all(not t.is_inbox for t in snap.targets)
    assert "no-such-id" in caplog.text
    assert "disabled" in caplog.text.lower()


async def test_two_flagged_targets_lowest_id_wins_with_warning(fake, tmp_path, caplog):
    d = Descriptions(tmp_path / "t.yaml")
    d.save({"ds-buy": TargetMeta(inbox=True), "ds-shops": TargetMeta(inbox=True)})
    disco = Discovery(fake, d, items_per_target=10, ttl_s=60)
    with caplog.at_level(logging.WARNING):
        snap = await disco.refresh()
    assert snap.target("ds-buy").is_inbox is True  # "ds-buy" < "ds-shops"
    assert snap.target("ds-shops").is_inbox is False
    assert "inbox" in caplog.text.lower()


async def test_invalidation_during_an_in_flight_refresh_is_not_swallowed(fake, tmp_path):
    """The admin page saves `targets.yaml` from its own HTTP thread and calls invalidate() to
    make the change visible without a restart. If that lands while a refresh is already running,
    the refresh must not clear the flag it never saw: the snapshot it is about to store predates
    the save, so the *next* get() has to refetch rather than serve it for a whole TTL."""
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=600, clock=lambda: t["now"])
    first = await disco.get()

    search = fake.search
    invalidated_mid_flight = {"done": False}

    async def search_then_invalidate(*args, **kwargs):
        results = await search(*args, **kwargs)
        if not invalidated_mid_flight["done"]:
            invalidated_mid_flight["done"] = True
            disco.invalidate()  # arrives while this very refresh is still running
        return results

    fake.search = search_then_invalidate
    disco.invalidate()
    second = await disco.get()
    assert second is not first

    third = await disco.get()  # well inside the TTL: only the surviving flag can force this
    assert third is not second


async def test_local_only_flag_from_yaml_reaches_the_snapshot(fake, tmp_path):
    d = Descriptions(tmp_path / "targets.yaml")
    d.save({"ds-buy": TargetMeta(local_only=True)})
    snap = await Discovery(fake, d, items_per_target=10, ttl_s=60).refresh()
    assert snap.target("ds-buy").local_only is True
    assert snap.target("ds-shops").local_only is False


async def test_option_descriptions_and_hidden_flag_reach_the_snapshot(fake, tmp_path):
    d = Descriptions(tmp_path / "targets.yaml")
    d.save({"ds-buy": TargetMeta(hidden=True, fields={
        "shop": FieldMeta(options={"o1": "супермаркет у дома"})})})
    snap = await Discovery(fake, d, items_per_target=10, ttl_s=60).refresh()
    shop = snap.target("ds-buy").field("shop")
    assert [(o.name, o.description) for o in shop.options] == [
        ("Rimi", "супермаркет у дома"), ("Prisma", "")]
    assert snap.target("ds-buy").hidden is True and snap.target("ds-shops").hidden is False


async def test_workspace_root_is_offered_as_a_page_target_when_enabled(fake, tmp_path):
    from app import texts
    from app.notion.mapper import WORKSPACE_ROOT_ID

    d = Descriptions(tmp_path / "t.yaml")
    snap = await Discovery(fake, d, items_per_target=10, ttl_s=60, workspace_root=True).refresh()
    root = snap.target(WORKSPACE_ROOT_ID)
    assert root is not None and root.kind == "page" and root.name == texts.ROOT_TARGET_NAME
    assert root.operations == frozenset({"create_page"})
    assert d.load()[WORKSPACE_ROOT_ID].name == texts.ROOT_TARGET_NAME  # hideable like any other
    plain = await Discovery(fake, d, items_per_target=10, ttl_s=60).refresh()
    assert plain.target(WORKSPACE_ROOT_ID) is None


async def test_a_new_row_is_patched_into_the_cached_snapshot(disco, fake):
    """A plan's next step may name what the step before it created; patching the one row in
    beats re-reading the whole workspace after every write."""
    await disco.get()
    before = len(fake.calls)

    disco.note_new_item("ds-buy", "row-new", "Кефир", "https://notion.so/row-new")
    snap = await disco.get()

    assert [i.title for i in snap.target("ds-buy").items][0] == "Кефир"
    assert fake.calls[before:] == []  # nothing was refetched
    disco.note_new_item("ds-buy", "row-new", "Кефир")  # twice is once
    assert [i.title for i in snap.target("ds-buy").items].count("Кефир") == 1


async def test_patching_keeps_the_item_limit(disco):
    snap = await disco.get()
    for n in range(15):
        disco.note_new_item("ds-buy", f"row-{n}", f"Товар {n}")
    assert len(snap.target("ds-buy").items) == 10  # items_per_target


async def test_a_write_that_cannot_be_patched_falls_back_to_a_refetch(disco, fake):
    await disco.get()
    before = len(fake.calls)

    disco.note_new_item("ds-unknown", "row-new", "Кефир")
    await disco.get()

    assert fake.calls[before:] != []  # the whole snapshot was read again


async def test_a_refresh_behind_an_answer_does_not_hold_up_the_next_message(fake, tmp_path):
    """The whole point: a message that arrives with a stale snapshot is answered from it."""
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    first = await disco.get()
    t["now"] += timedelta(seconds=61)

    before = len(fake.calls)
    served = [await disco.get() for _ in range(3)]

    assert all(s is first for s in served)
    assert len(fake.calls) == before  # nothing was fetched while they were being served
    await disco.settled()
    assert sum(1 for c in fake.calls if c[0] == "search") == 2  # exactly one refresh, behind


async def test_a_failed_refresh_behind_an_answer_keeps_the_old_snapshot(fake, tmp_path):
    t = {"now": datetime(2026, 9, 9, 12, 0, tzinfo=UTC)}
    disco = Discovery(fake, Descriptions(tmp_path / "t.yaml"), ttl_s=60, clock=lambda: t["now"])
    first = await disco.get()
    t["now"] += timedelta(seconds=61)
    fake.fail_search = NotionError(500, "server_error", "boom")

    assert (await disco.get()) is first
    await disco.settled()
    assert (await disco.get()) is first  # still usable; nothing raised at the user


async def test_a_page_a_step_created_is_a_place_the_next_step_can_name(disco):
    """Live: a plan created «Виды ворот тории» and its second step, unable to find that page,
    let the model choose — and it filed the contents under an unrelated borsch recipe."""
    snap = await disco.get()
    assert snap.target("new-page") is None

    disco.note_new_page("new-page", "Виды ворот тории", "ideas", "https://notion.so/new-page")

    made = snap.target("new-page")
    assert made is not None and made.kind == "page"
    assert made.name == "Виды ворот тории" and made.parent_page_id == "ideas"
    assert made.path.endswith("Виды ворот тории") and "append" in made.operations


async def test_a_page_with_nowhere_to_put_it_falls_back_to_a_refetch(disco, fake):
    await disco.get()
    disco.note_new_page("", "", "", "")
    before = len(fake.calls)
    await disco.get()
    assert len(fake.calls) > before
