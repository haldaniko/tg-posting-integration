from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import httpx

from .config import SiteConfig


@dataclass(frozen=True)
class Option:
    slug: str
    name: str


@dataclass(frozen=True)
class Options:
    categories: tuple[Option, ...]
    authors: tuple[Option, ...]


@dataclass(frozen=True)
class Article:
    article_id: str
    status: str
    url: str
    image_url: str | None


class SiteError(Exception):
    def __init__(self, message: str, *, retryable: bool = False, status: int | None = None):
        super().__init__(message)
        self.retryable = retryable
        self.status = status


class SitePublisher(Protocol):
    async def options(self) -> Options: ...
    async def get_article(self, external_id: str) -> Article | None: ...
    async def create_article(self, external_id: str, payload: dict) -> Article: ...
    async def upload_image(self, external_id: str, path: Path) -> Article: ...
    async def publish_article(self, external_id: str) -> Article: ...
    async def close(self) -> None: ...


class FxlfmPublisher:
    def __init__(self, site: SiteConfig, client: httpx.AsyncClient | None = None):
        self.site = site
        self.client = client or httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=5.0))
        self.owns_client = client is None

    async def close(self) -> None:
        if self.owns_client:
            await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs) -> dict | None:
        url = self.site.api_base + path
        headers = {"Authorization": f"Bearer {self.site.token}"}
        if "json" in kwargs:
            headers["Content-Type"] = "application/json"
        try:
            response = await self.client.request(method, url, headers=headers, **kwargs)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise SiteError("Сайт временно недоступен. Повторите действие.", retryable=True) from exc
        if response.status_code == 404 and method == "GET" and path.startswith("articles/"):
            return None
        if response.status_code >= 400:
            labels = {400: "Сайт отклонил данные статьи или фото.", 403: "Сайт отклонил ключ интеграции. Обратитесь к администратору.", 404: "Черновик не найден на сайте. Повторите действие.", 409: "Конфликт статьи на сайте. Обратитесь к администратору."}
            raise SiteError(labels.get(response.status_code, "Ошибка сайта. Повторите действие."), retryable=response.status_code >= 500, status=response.status_code)
        try:
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except ValueError as exc:
            raise SiteError("Сайт вернул неожиданный ответ.", retryable=True) from exc

    @staticmethod
    def _article(data: dict | None) -> Article | None:
        if data is None:
            return None
        try:
            return Article(str(data["article_id"]), str(data["status"]), str(data["url"]), data.get("image_url"))
        except (KeyError, TypeError) as exc:
            raise SiteError("Сайт вернул неполные данные статьи.", retryable=True) from exc

    async def options(self) -> Options:
        data = await self._request("GET", "options/")
        try:
            if data["version"] != 1:
                raise ValueError
            categories = tuple(Option(str(x["slug"]), str(x["name"])) for x in data["categories"])
            authors = tuple(Option(str(x["slug"]), str(x["name"])) for x in data["authors"])
            return Options(categories, authors)
        except (KeyError, TypeError, ValueError) as exc:
            raise SiteError("Сайт вернул неподдерживаемые варианты категорий.") from exc

    async def get_article(self, external_id: str) -> Article | None:
        return self._article(await self._request("GET", f"articles/{external_id}/"))

    async def create_article(self, external_id: str, payload: dict) -> Article:
        data = await self._request("POST", "articles/", json={"external_id": external_id, **payload})
        return self._article(data)

    async def upload_image(self, external_id: str, path: Path) -> Article:
        kind = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "application/octet-stream")
        try:
            with path.open("rb") as file:
                data = await self._request("PUT", f"articles/{external_id}/image/", files={"image": (path.name, file, kind)})
        except FileNotFoundError as exc:
            raise SiteError("Локальное фото утрачено. Выберите новое фото в новой версии статьи.") from exc
        return self._article(data)

    async def publish_article(self, external_id: str) -> Article:
        data = await self._request("POST", f"articles/{external_id}/publish/")
        return self._article(data)
