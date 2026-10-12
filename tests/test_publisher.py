import asyncio
from uuid import uuid4

import httpx

from posting_bot.config import SiteConfig
from posting_bot.publisher import FxlfmPublisher, SiteError


def test_fxlfm_http_contract(tmp_path):
    external_id = str(uuid4())
    photo = b"\xff\xd8\xffbinary"
    calls = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer secret"
        assert request.url.path.endswith("/")
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("options/"):
            return httpx.Response(200, json={"version": 1, "site": "fxlfm", "categories": [{"slug": "politics", "name": "Politics"}], "authors": []})
        if request.url.path.endswith(f"articles/{external_id}/"):
            return httpx.Response(404)
        if request.url.path.endswith("articles/"):
            assert request.headers["content-type"] == "application/json"
            assert request.read().decode().count(external_id) == 1
            return httpx.Response(201, json={"article_id": 5, "status": "draft", "url": "https://test/a", "image_url": None})
        if request.url.path.endswith("image/"):
            assert b'name="image"' in request.read()
            return httpx.Response(200, json={"article_id": 5, "status": "draft", "url": "https://test/a", "image_url": "https://test/i"})
        return httpx.Response(200, json={"article_id": 5, "status": "published", "url": "https://test/a", "image_url": "https://test/i"})

    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        pub = FxlfmPublisher(SiteConfig("fxlfm", "fxlfm", "https://test/api/bot/v1/", "secret"), client)
        assert (await pub.options()).categories[0].slug == "politics"
        assert await pub.get_article(external_id) is None
        assert (await pub.create_article(external_id, {"title": "T", "content": "C", "category_slugs": ["politics"]})).article_id == "5"
        assert (await pub.upload_image(external_id, "telegram.jpg", photo)).image_url == "https://test/i"
        assert (await pub.publish_article(external_id)).status == "published"
        await client.aclose()

    asyncio.run(run())
    assert len(calls) == 5


def test_fxlfm_error_does_not_leak_token():
    async def run():
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(403, text="secret")))
        pub = FxlfmPublisher(SiteConfig("fxlfm", "fxlfm", "https://test/", "secret"), client)
        try:
            await pub.options()
        except SiteError as exc:
            assert exc.status == 403
            assert "secret" not in str(exc)
        else:
            assert False
        await client.aclose()
    asyncio.run(run())
