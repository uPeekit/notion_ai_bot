import httpx
import pytest

from app.notion import images
from app.notion.images import MAX_IMAGE_BYTES, ImageHost, NotAnImage
from tests.fakes import FakeNotionProvider

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 100


@pytest.fixture
def public(monkeypatch):
    """Every host counts as public except `internal.lan`: DNS is not something tests touch."""
    async def _public(url: str) -> bool:
        return httpx.URL(url).host != "internal.lan"

    monkeypatch.setattr(images, "_public", _public)


def host(handler) -> tuple[ImageHost, FakeNotionProvider]:
    notion = FakeNotionProvider()
    return ImageHost(notion, transport=httpx.MockTransport(handler)), notion


async def test_private_and_non_http_addresses_are_not_public():
    for url in ("http://127.0.0.1:8787/", "http://10.0.0.1/x.png", "http://192.168.1.1/a.jpg",
                "http://[::1]/a.png", "ftp://example.com/a.png", "file:///etc/passwd"):
        assert await images._public(url) is False, url


async def test_an_image_is_uploaded_under_a_safe_name(public):
    h, notion = host(lambda req: httpx.Response(200, content=PNG,
                                                headers={"content-type": "image/png"}))
    upload_id = await h.host("https://example.com/pics/Скамья из дуба.png?x=1")
    assert upload_id is not None
    call = next(c for c in notion.calls if c[0] == "upload_file")
    assert call[1].endswith(".png") and "/" not in call[1] and " " not in call[1]
    assert call[2:] == ("image/png", len(PNG))


async def test_a_web_page_is_not_an_image(public):
    h, notion = host(lambda req: httpx.Response(200, text="<html>",
                                                headers={"content-type": "text/html"}))
    assert await h.host("https://example.com/page") is None
    assert not any(c[0] == "upload_file" for c in notion.calls)


async def test_an_oversized_file_is_refused(public):
    big = b"0" * (MAX_IMAGE_BYTES + 1)
    h, _ = host(lambda req: httpx.Response(200, content=big,
                                           headers={"content-type": "image/jpeg"}))
    with pytest.raises(NotAnImage, match="5 MB"):
        await h.download("https://example.com/huge.jpg")


async def test_a_redirect_into_the_local_network_is_refused(public):
    def handler(req):
        if req.url.host == "example.com":
            return httpx.Response(302, headers={"location": "http://internal.lan/secret.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    h, _ = host(handler)
    with pytest.raises(NotAnImage, match="public"):
        await h.download("https://example.com/redirect.png")


async def test_a_redirect_to_a_public_image_is_followed(public):
    def handler(req):
        if req.url.path == "/old.png":
            return httpx.Response(301, headers={"location": "/new.png"})
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    h, _ = host(handler)
    data, ctype = await h.download("https://example.com/old.png")
    assert (data, ctype) == (PNG, "image/png")


async def test_a_missing_file_is_skipped(public):
    h, _ = host(lambda req: httpx.Response(404))
    assert await h.host("https://example.com/gone.jpg") is None
