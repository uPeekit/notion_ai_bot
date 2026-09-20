"""The per-turn Notion tally behind the "done:" log line."""

import asyncio

import httpx

from app.notion import stats
from app.notion.direct import DirectNotionProvider


def test_calls_outside_a_turn_are_not_counted():
    stats.record("GET", "/pages/abc")  # startup checks, the sweeper: nothing is summarising
    with stats.collect() as tally:
        pass
    assert tally == {} and stats.summary(tally) == ""


def test_calls_are_counted_by_method_and_collection():
    with stats.collect() as tally:
        stats.record("GET", "/pages/abc")
        stats.record("GET", "/pages/def")
        stats.record("POST", "/data_sources/x/query")
        stats.record("PATCH", "/blocks/b/children?page_size=100")
    assert tally == {"get pages": 2, "post data_sources": 1, "patch blocks": 1}
    assert stats.summary(tally) == "4 calls: get pages 2, patch blocks 1, post data_sources 1"


async def test_calls_made_inside_gather_count_against_the_turn_that_started_it():
    """asyncio.gather copies the context, so the tally has to be a mutable object shared by
    those copies — research does its text and image passes exactly this way."""
    async def work(n):
        stats.record("POST", "/search")

    with stats.collect() as tally:
        await asyncio.gather(*(work(i) for i in range(3)))
    assert tally == {"post search": 3}


async def test_a_real_request_counts_itself_once_including_its_retries():
    attempts = []

    def handler(req: httpx.Request):
        attempts.append(req)
        if len(attempts) == 1:
            return httpx.Response(500, json={})
        return httpx.Response(200, json={"object": "user", "id": "u1"})

    with stats.collect() as tally:
        async with DirectNotionProvider("secret", transport=httpx.MockTransport(handler),
                                        max_retries=1) as p:
            await p.me()

    assert len(attempts) == 2  # the 500 was retried
    assert tally == {"get users": 1}  # one call as the user asked for it, not two
