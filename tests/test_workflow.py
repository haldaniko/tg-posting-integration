import asyncio

from posting_bot.config import Config, SiteConfig
from posting_bot.publisher import Article, Option, Options, SiteError
from posting_bot.storage import Draft, Store
from posting_bot.workflow import BotService, PhotoRef


class FakePublisher:
    def __init__(self):
        self.articles = {}
        self.creates = 0
        self.publishes = 0
        self.fail_image = False
        self.timeout_after_create = False
        self.delay = 0

    async def options(self):
        return Options((Option("politics", "Politics"),), (Option("editor", "Editor"),))

    async def get_article(self, external_id):
        return self.articles.get(external_id)

    async def create_article(self, external_id, payload):
        if self.delay:
            await asyncio.sleep(self.delay)
        self.creates += 1
        current = self.articles.get(external_id)
        if current:
            assert current.status != "published"
            return current
        assert payload["category_slugs"] == ["politics"]
        article = Article("7", "draft", "https://test/article/7", None)
        self.articles[external_id] = article
        if self.timeout_after_create:
            self.timeout_after_create = False
            raise SiteError("Сайт временно недоступен", retryable=True)
        return article

    async def upload_image(self, external_id, filename, data):
        if self.fail_image:
            raise SiteError("Фото не загружено", retryable=True)
        assert filename == "telegram.jpg"
        assert data == b"\xff\xd8\xffphoto"
        article = Article("7", "draft", "https://test/article/7", "https://test/image/7")
        self.articles[external_id] = article
        return article

    async def publish_article(self, external_id):
        self.publishes += 1
        article = self.articles[external_id]
        article = Article(article.article_id, "published", article.url, article.image_url)
        self.articles[external_id] = article
        return article

    async def close(self):
        pass


def setup(tmp_path):
    site = SiteConfig("fxlfm", "fxlfm", "https://test/api/bot/v1/", "secret")
    config = Config("telegram-secret", frozenset({1}), f"sqlite:///{tmp_path / 'bot.db'}", (site,))
    store = Store(config.database_url)
    fake = FakePublisher()
    async def load_photo(file_id):
        assert file_id == "telegram-file-id"
        return b"\xff\xd8\xffphoto"
    return BotService(config, store, {"fxlfm": fake}, load_photo), fake


def click(service, view, label, user=1):
    data = next(data for line in view.buttons for title, data in line if title == label)
    return asyncio.run(service.callback(user, data))


def build_preview(service, photo=False):
    view = service.start(1, fresh=True)
    view = click(service, view, "fxlfm")
    view = asyncio.run(service.input(1, "Title", update_id=10))
    view = click(service, view, "Пропустить")
    view = asyncio.run(service.input(1, "First paragraph", update_id=11))
    view = asyncio.run(service.input(1, "Second paragraph", update_id=12))
    view = click(service, view, "Текст готов")
    if photo:
        view = asyncio.run(service.input(1, PhotoRef("telegram-file-id", ".jpg"), update_id=13))
    else:
        view = click(service, view, "Пропустить фото")
    view = click(service, view, "Politics")
    view = click(service, view, "Без автора")
    view = click(service, view, "Без тегов")
    assert "First paragraph\n\nSecond paragraph" in view.text
    return view


def test_allowlist_and_recovery(tmp_path):
    service, fake = setup(tmp_path)
    assert "Нет доступа" in service.start(2, fresh=True).text
    view = service.start(1, fresh=True)
    assert "Нет доступа" in asyncio.run(service.callback(2, view.buttons[0][0][1])).text
    view = click(service, view, "fxlfm")
    view = asyncio.run(service.input(1, "Title", update_id=10))
    assert "уже обработано" in asyncio.run(service.input(1, "Title", update_id=10)).text
    restarted = BotService(service.config, Store(service.config.database_url), {"fxlfm": fake})
    assert "описание" in restarted.start(1).text


def test_edit_stale_and_publish_once(tmp_path):
    service, fake = setup(tmp_path)
    view = build_preview(service)
    old_publish = next(data for line in view.buttons for label, data in line if label == "Опубликовать")
    edit = click(service, view, "Изменить")
    assert "устарела" in asyncio.run(service.callback(1, old_publish)).text
    edit = click(service, edit, "Заголовок")
    view = asyncio.run(service.input(1, "Revised title", update_id=20))
    result = click(service, view, "Опубликовать")
    assert "https://test/article/7" in result.text
    assert fake.publishes == 1
    assert "устарела" in asyncio.run(service.callback(1, old_publish)).text
    with service.store.session() as db:
        draft = db.query(Draft).first()
        assert draft.deliveries[0].article_id == "7"
        assert draft.deliveries[0].published_at is not None
    listing = service.drafts(1)
    opened = click(service, listing, "fxlfm · Revised title · published")
    cloned = click(service, opened, "Новая статья из этой")
    assert "новая статья" in cloned.text.lower()
    with service.store.session() as db:
        drafts = db.query(Draft).all()
        assert len(drafts) == 2
        assert drafts[0].deliveries[0].external_id != drafts[1].deliveries[0].external_id


def test_photo_failure_then_retry(tmp_path):
    service, fake = setup(tmp_path)
    view = build_preview(service, photo=True)
    fake.fail_image = True
    failed = click(service, view, "Опубликовать")
    assert "Фото не загружено" in failed.text
    assert fake.publishes == 0
    fake.fail_image = False
    result = click(service, failed, "Повторить")
    assert "Статья опубликована" in result.text
    assert fake.publishes == 1
    with service.store.session() as db:
        assert db.query(Draft).first().photo_file_id is None
    assert not list(tmp_path.glob("*.jpg"))


def test_timeout_after_create_recovers_same_external_id(tmp_path):
    service, fake = setup(tmp_path)
    view = build_preview(service)
    fake.timeout_after_create = True
    result = click(service, view, "Опубликовать")
    assert "Статья опубликована" in result.text
    assert fake.creates == 2
    with service.store.session() as db:
        original_id = db.query(Draft).first().deliveries[0].external_id
    assert len(fake.articles) == 1
    assert original_id in fake.articles


def test_old_cancel_cannot_cancel_another_draft(tmp_path):
    service, fake = setup(tmp_path)
    old = build_preview(service)
    old_cancel = next(data for line in old.buttons for label, data in line if label == "Отмена")
    assert service.cancel(1).text == "Черновик отменён."
    new = service.start(1, fresh=True)
    assert "устарела" in asyncio.run(service.callback(1, old_cancel)).text
    assert service.active_state(1) == "site"


def test_concurrent_publish_clicks_are_idempotent(tmp_path):
    service, fake = setup(tmp_path)
    view = build_preview(service)
    fake.delay = 0.02
    data = next(data for line in view.buttons for label, data in line if label == "Опубликовать")

    async def both():
        return await asyncio.gather(service.callback(1, data), service.callback(1, data))

    results = asyncio.run(both())
    assert any("опубликована" in result.text for result in results)
    assert fake.publishes == 1
    assert len(fake.articles) == 1


def test_invalid_telegram_photo_never_publishes(tmp_path):
    service, fake = setup(tmp_path)
    async def invalid_photo(_):
        return b"not an image"
    service.photo_loader = invalid_photo
    view = build_preview(service, photo=True)
    failed = click(service, view, "Опубликовать")
    assert "JPG, PNG или WebP" in failed.text
    assert fake.publishes == 0
    assert len(fake.articles) == 1  # Only an unpublished site draft was created.

