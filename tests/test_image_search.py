import httpx
import pytest

from app.llm import image_search
from app.llm.image_search import ImageSearch


@pytest.fixture
def public(monkeypatch):
    async def _public(url):
        return True

    monkeypatch.setattr(image_search, "is_public_url", _public)


def search(handler) -> ImageSearch:
    return ImageSearch(transport=httpx.MockTransport(handler))


async def test_commons_returns_thumbnails_with_readable_captions():
    def handler(req):
        assert req.url.host == "commons.wikimedia.org"
        assert "torii gate filetype:bitmap" == req.url.params["gsrsearch"]
        assert req.headers["user-agent"].startswith("notion-ai-bot/")
        return httpx.Response(200, json={"query": {"pages": {
            "2": {"index": 2, "title": "File:Second_one.png",
                  "imageinfo": [{"url": "https://up/2.png", "thumburl": "https://up/2t.png"}]},
            "1": {"index": 1, "title": "File:Itsukushima_Gate.jpg",
                  "imageinfo": [{"url": "https://up/1.jpg"}]},
        }}})

    hits = await search(handler).commons("torii gate")
    assert hits == [("https://up/1.jpg", "Itsukushima Gate"), ("https://up/2t.png", "Second one")]


async def test_commons_failure_is_no_pictures():
    assert await search(lambda req: httpx.Response(503)).commons("x") == []


async def test_og_image_is_read_from_the_page_head(public):
    html = ('<html><head><title>Тории — Википедия</title>'
            '<meta property="og:image" content="/img/torii.jpg?a=1&amp;b=2"></head></html>')
    s = search(lambda req: httpx.Response(200, text=html,
                                          headers={"content-type": "text/html; charset=utf-8"}))
    assert await s.og_image("https://ru.wikipedia.org/wiki/Torii") == (
        "https://ru.wikipedia.org/img/torii.jpg?a=1&b=2", "Тории — Википедия")


async def test_og_image_missing_or_not_html_is_none(public):
    s = search(lambda req: httpx.Response(200, text="<html></html>",
                                          headers={"content-type": "text/html"}))
    assert await s.og_image("https://a.example/") is None
    s = search(lambda req: httpx.Response(200, content=b"%PDF",
                                          headers={"content-type": "application/pdf"}))
    assert await s.og_image("https://a.example/x.pdf") is None


async def test_og_image_never_fetches_a_private_address():
    called = []
    s = search(lambda req: called.append(req) or httpx.Response(200))
    assert await s.og_image("http://192.168.1.1/") is None and called == []
