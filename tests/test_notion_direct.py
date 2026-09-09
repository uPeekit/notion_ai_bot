import json

import httpx
import pytest

from app.notion.direct import DirectNotionProvider
from app.notion.errors import NotionError, NotionUnavailable


def make(handler, **kw):
    return DirectNotionProvider("secret", transport=httpx.MockTransport(handler), **kw)


async def test_headers_and_me():
    seen = {}

    def handler(req: httpx.Request):
        seen.update(dict(req.headers))
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"object": "user", "id": "u1"})

    async with make(handler) as p:
        assert (await p.me())["id"] == "u1"
    assert seen["authorization"] == "Bearer secret"
    assert seen["notion-version"] == "2025-09-03"
    assert seen["url"] == "https://api.notion.com/v1/users/me"


async def test_search_paginates_and_filters():
    calls = []

    def handler(req: httpx.Request):
        body = json.loads(req.content)
        calls.append(body)
        if body.get("start_cursor") is None:
            return httpx.Response(200, json={"results": [{"id": "a"}], "has_more": True,
                                             "next_cursor": "c2"})
        return httpx.Response(200, json={"results": [{"id": "b"}], "has_more": False,
                                         "next_cursor": None})

    async with make(handler) as p:
        res = await p.search(object_type="data_source")
    assert [r["id"] for r in res] == ["a", "b"]
    assert calls[0]["filter"] == {"property": "object", "value": "data_source"}
    assert calls[0]["page_size"] == 100
    assert calls[1]["start_cursor"] == "c2"


async def test_query_data_source_uses_patch_and_body():
    seen = {}

    def handler(req: httpx.Request):
        seen["method"] = req.method
        seen["url"] = str(req.url)
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"results": [{"id": "p"}], "has_more": False})

    async with make(handler) as p:
        res = await p.query_data_source("ds1", sorts=[{"timestamp": "last_edited_time",
                                                       "direction": "descending"}], page_size=7)
    assert res == [{"id": "p"}]
    assert seen["method"] == "PATCH"
    assert seen["url"].endswith("/v1/data_sources/ds1/query")
    assert seen["body"] == {"sorts": [{"timestamp": "last_edited_time",
                                       "direction": "descending"}], "page_size": 7}


async def test_create_update_append_delete_shapes():
    seen = []

    def handler(req: httpx.Request):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        return httpx.Response(200, json={"id": "x"})

    async with make(handler) as p:
        await p.create_page({"type": "data_source_id", "data_source_id": "ds"}, {"T": {}},
                            children=[{"object": "block"}])
        await p.update_page("pg", properties={"A": {}})
        await p.update_page("pg", archived=True)
        await p.append_blocks("blk", [{"object": "block"}])
        await p.delete_block("blk")
        await p.get_database("db")
        await p.get_data_source("ds")
        await p.get_page("pg")
    assert seen[0] == ("POST", "/v1/pages", {"parent": {"type": "data_source_id",
                                                        "data_source_id": "ds"},
                                             "properties": {"T": {}},
                                             "children": [{"object": "block"}]})
    assert seen[1] == ("PATCH", "/v1/pages/pg", {"properties": {"A": {}}})
    assert seen[2] == ("PATCH", "/v1/pages/pg", {"archived": True})
    assert seen[3] == ("PATCH", "/v1/blocks/blk/children", {"children": [{"object": "block"}]})
    assert seen[4] == ("DELETE", "/v1/blocks/blk", None)
    assert seen[5] == ("GET", "/v1/databases/db", None)
    assert seen[6] == ("GET", "/v1/data_sources/ds", None)
    assert seen[7] == ("GET", "/v1/pages/pg", None)


async def test_4xx_raises_notion_error_without_token():
    def handler(req):
        return httpx.Response(400, json={"code": "validation_error", "message": "bad prop"})

    async with make(handler) as p:
        with pytest.raises(NotionError) as ei:
            await p.get_page("pg")
    assert ei.value.status == 400
    assert ei.value.code == "validation_error"
    assert "secret" not in str(ei.value)


async def test_429_retries_with_retry_after(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429, headers={"Retry-After": "2"},
                                  json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"id": "ok"})

    async with make(handler) as p:
        assert (await p.get_page("pg"))["id"] == "ok"
    assert sleeps == [2.0, 2.0]


async def test_429_exhausted_raises(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)

    def handler(req):
        return httpx.Response(429, json={"code": "rate_limited", "message": "slow"})

    async with make(handler, max_retries=2) as p:
        with pytest.raises(NotionError) as ei:
            await p.get_page("pg")
    assert ei.value.status == 429


async def test_5xx_and_network_become_unavailable(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)

    def handler(req):
        return httpx.Response(502, text="bad gateway")

    async with make(handler) as p:
        with pytest.raises(NotionUnavailable):
            await p.get_page("pg")

    def boom(req):
        raise httpx.ConnectError("no route")

    async with make(boom) as p:
        with pytest.raises(NotionUnavailable):
            await p.get_page("pg")


async def test_retry_after_http_date_falls_back_to_backoff(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 3:
            return httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"},
                                  json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"id": "ok"})

    async with make(handler) as p:
        assert (await p.get_page("pg"))["id"] == "ok"
    assert sleeps == [2.0, 4.0]


async def test_retry_after_is_clamped(monkeypatch):
    sleeps = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 2:
            return httpx.Response(429, headers={"Retry-After": "3600"},
                                  json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"id": "ok"})

    async with make(handler) as p:
        assert (await p.get_page("pg"))["id"] == "ok"
    assert sleeps == [30.0]


async def test_5xx_retries_exactly_twice(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(502, text="bad gateway")

    async with make(handler) as p:
        with pytest.raises(NotionUnavailable):
            await p.get_page("pg")
    assert n["i"] == 3


async def test_429_does_not_consume_5xx_budget(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}
    codes = [429, 502, 502, 200]

    def handler(req):
        code = codes[n["i"]]
        n["i"] += 1
        if code == 200:
            return httpx.Response(200, json={"id": "ok"})
        if code == 429:
            return httpx.Response(429, json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(502, text="bad gateway")

    async with make(handler) as p:
        assert (await p.get_page("pg"))["id"] == "ok"
    assert n["i"] == 4


async def test_post_not_retried_on_network_error(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def boom(req):
        n["i"] += 1
        raise httpx.ConnectError("no route")

    async with make(boom) as p:
        with pytest.raises(NotionUnavailable):
            await p.create_page({"type": "data_source_id", "data_source_id": "ds"}, {"T": {}})
    assert n["i"] == 1


async def test_post_not_retried_on_5xx(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(502, text="bad gateway")

    async with make(handler) as p:
        with pytest.raises(NotionUnavailable):
            await p.create_page({"type": "data_source_id", "data_source_id": "ds"}, {"T": {}})
    assert n["i"] == 1


async def test_post_still_retried_on_429(monkeypatch):
    async def fake_sleep(s):
        pass

    monkeypatch.setattr("app.notion.direct.asyncio.sleep", fake_sleep)
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] < 2:
            return httpx.Response(429, json={"code": "rate_limited", "message": "slow"})
        return httpx.Response(200, json={"id": "new-page"})

    async with make(handler) as p:
        res = await p.create_page({"type": "data_source_id", "data_source_id": "ds"}, {"T": {}})
    assert res["id"] == "new-page"
    assert n["i"] == 2
