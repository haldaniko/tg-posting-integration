from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable
from uuid import UUID

from sqlalchemy import select

from .config import Config
from .publisher import Options, SiteError, SitePublisher
from .storage import Delivery, Draft, Store, utcnow

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class View:
    text: str
    buttons: tuple[tuple[tuple[str, str], ...], ...] = ()


@dataclass(frozen=True)
class PhotoRef:
    file_id: str
    suffix: str


def row(*pairs: tuple[str, str]) -> tuple[tuple[str, str], ...]:
    return pairs


class BotService:
    def __init__(self, config: Config, store: Store, publishers: dict[str, SitePublisher], photo_loader: Callable[[str], Awaitable[bytes]] | None = None):
        self.config = config
        self.store = store
        self.publishers = publishers
        self.sites = {s.id: s for s in config.sites}
        self.photo_loader = photo_loader

    def allowed(self, user_id: int | None) -> bool:
        return user_id is not None and user_id in self.config.allowed_user_ids

    def active_state(self, user_id: int) -> str | None:
        with self.store.session() as db:
            draft = self.store.active(db, user_id)
            return draft.state if draft else None

    def _view(self, draft: Draft) -> View:
        prefix = f"x:{UUID(draft.id).hex}:{draft.revision}:"
        def b(label: str, action: str) -> tuple[str, str]:
            return label, prefix + action
        state = draft.state
        if state == "site":
            return View("Выберите сайт для новой статьи:", tuple(row(b(site.name, f"s{i}")) for i, site in enumerate(self.config.sites)))
        if state == "title":
            return View("Отправьте заголовок (до 300 символов).")
        if state == "summary":
            return View("Отправьте краткое описание или пропустите.", (row(b("Пропустить", "skip")),))
        if state == "content":
            return View(f"Отправляйте основной текст несколькими сообщениями. Сейчас {len(draft.content)} символов.", (row(b("Текст готов", "done")),))
        if state == "photo":
            return View("Отправьте фото JPG, PNG или WebP (до 10 МБ) или пропустите.", (row(b("Пропустить фото", "skip")),))
        if state in {"category", "author"}:
            key = "categories" if state == "category" else "authors"
            options = draft.option_snapshot.get(key, [])
            buttons = [row(b(item["name"], f"{'c' if state == 'category' else 'a'}{i}")) for i, item in enumerate(options)]
            if state == "author":
                buttons.append(row(b("Без автора", "skip")))
            return View("Выберите категорию:" if state == "category" else "Выберите автора или пропустите:", tuple(buttons))
        if state == "tags":
            return View("Отправьте теги через запятую (до 10, каждый до 80 символов) или пропустите.", (row(b("Без тегов", "skip")),))
        if state == "preview":
            preview = self._preview(draft)
            return View(preview + "\n\nПубликовать?", (row(b("Изменить", "edit"), b("Опубликовать", "pub")), row(b("Отмена", "cancel"))))
        if state == "edit_select":
            fields = [("Заголовок", "title"), ("Описание", "summary"), ("Текст", "content"), ("Фото", "photo"), ("Категория", "category"), ("Автор", "author"), ("Теги", "tags")]
            return View("Что изменить?", tuple(row(b(label, f"e{field}")) for label, field in fields) + (row(b("Назад", "back")),))
        if state == "publishing":
            return View("Публикация выполняется. Если ответ не пришёл, откройте /drafts и повторите.", (row(b("Повторить", "retry")),))
        if state == "failed":
            return View("Публикация не завершена. Можно повторить с тем же ID или создать новую версию для правок.", (row(b("Повторить", "retry"), b("Новая версия", "clone")), row(b("Отмена", "cancel"))))
        if state == "published":
            delivery = draft.deliveries[0]
            return View(f"Опубликовано: {delivery.url}\n\nПравки создадут новую статью.", (row(b("Новая статья из этой", "clone")),))
        return View("Черновик отменён. /new — новая статья.")

    @staticmethod
    def _preview(draft: Draft) -> str:
        excerpt = draft.content[:1200] + ("…" if len(draft.content) > 1200 else "")
        return (f"Предпросмотр — {draft.site_id}\n\n{draft.title}\n\nОписание: {draft.summary or '—'}"
                f"\n\n{excerpt}\n\nФото: {'да' if draft.photo_file_id else 'нет'}"
                f"\nКатегория: {draft.category_slug or '—'}\nАвтор: {draft.author_slug or '—'}"
                f"\nТеги: {', '.join(draft.tag_names) or '—'}")

    def start(self, user_id: int, *, fresh: bool = False) -> View:
        if not self.allowed(user_id):
            return View("Нет доступа к этому боту.")
        with self.store.session() as db, db.begin():
            current = self.store.active(db, user_id)
            if current and current.state not in {"cancelled", "published"}:
                return self._view(current)
            if fresh:
                return self._view(self.store.new(db, user_id))
            return View("Выберите действие: /new — создать статью, /drafts — черновики, /cancel — отменить.")

    def drafts(self, user_id: int) -> View:
        if not self.allowed(user_id):
            return View("Нет доступа к этому боту.")
        drafts = self.store.list_drafts(user_id)
        if not drafts:
            return View("Черновиков пока нет. /new — создать статью.")
        return View("Последние статьи и черновики:", tuple(row((f"{d.site_id or 'Новый сайт'} · {(d.title or 'Без названия')[:35]} · {d.state}", f"o:{UUID(d.id).hex}")) for d in drafts))

    def cancel(self, user_id: int, *, expected_draft_id: str | None = None) -> View:
        if not self.allowed(user_id):
            return View("Нет доступа к этому боту.")
        with self.store.session() as db, db.begin():
            draft = self.store.active(db, user_id)
            if not draft:
                return View("Активного черновика нет.")
            if expected_draft_id is not None and draft.id != expected_draft_id:
                return View("Кнопка устарела. Откройте /drafts.")
            if draft.state == "publishing":
                return View("Публикация уже началась. Проверьте результат через /drafts.")
            draft.state = "cancelled"
            draft.photo_file_id = None
            draft.photo_suffix = None
            draft.revision += 1
            self.store.editor(db, user_id).active_draft_id = None
        return View("Черновик отменён.")

    async def _snapshot(self, site_id: str) -> dict:
        options: Options = await self.publishers[site_id].options()
        return {"categories": [{"slug": o.slug, "name": o.name} for o in options.categories],
                "authors": [{"slug": o.slug, "name": o.name} for o in options.authors]}

    async def input(self, user_id: int, value: str | PhotoRef, *, update_id: int | None = None) -> View:
        if not self.allowed(user_id):
            return View("Нет доступа к этому боту.")
        with self.store.session() as db, db.begin():
            if update_id is not None and not self.store.mark_update(db, update_id):
                return View("Это сообщение уже обработано.")
            draft = self.store.active(db, user_id)
            if not draft:
                return View("Сначала нажмите «Создать статью» или /new.")
            state = draft.state
            if isinstance(value, PhotoRef) and state != "photo":
                return View("Сейчас ожидается другой шаг. " + self._view(draft).text)
            if isinstance(value, str) and state not in {"title", "summary", "content", "tags"}:
                return View("Сообщение сейчас не ожидается. " + self._view(draft).text, self._view(draft).buttons)
            if isinstance(value, str):
                value = value.strip()
                if not value:
                    return View("Пустое сообщение не подходит. " + self._view(draft).text, self._view(draft).buttons)
            if state == "title":
                if len(value) > 300:
                    return View("Заголовок должен быть до 300 символов.")
                draft.title = value
                self._advance(draft, "summary")
            elif state == "summary":
                if len(value) > 1000:
                    return View("Описание должно быть до 1000 символов.")
                draft.summary = value
                self._advance(draft, "content")
            elif state == "content":
                if len(draft.content) + len(value) > 100_000:
                    return View("Текст слишком длинный (максимум 100 000 символов).")
                draft.content = draft.content + ("\n\n" if draft.content else "") + value
            elif state == "photo":
                draft.photo_file_id = value.file_id
                draft.photo_suffix = value.suffix
                self._advance(draft, "category")
            elif state == "tags":
                tags = [part.strip() for part in value.split(",") if part.strip()]
                if len(tags) > 10 or any(len(t) > 80 for t in tags):
                    return View("Допустимо до 10 тегов, каждый до 80 символов.")
                draft.tag_names = tags
                self._advance(draft, "preview")
            draft.revision += 1
            return self._view(draft)

    @staticmethod
    def _advance(draft: Draft, next_state: str) -> None:
        if draft.edit_field:
            draft.edit_field = None
            draft.state = "preview"
        else:
            draft.state = next_state

    async def callback(self, user_id: int, data: str) -> View:
        if not self.allowed(user_id):
            return View("Нет доступа к этому боту.")
        if data.startswith("o:"):
            try:
                draft_id = str(UUID(hex=data[2:]))
            except ValueError:
                return View("Кнопка устарела.")
            with self.store.session() as db, db.begin():
                draft = db.get(Draft, draft_id)
                if not draft or draft.user_id != user_id:
                    return View("Черновик недоступен.")
                if draft.state not in {"published", "cancelled"}:
                    self.store.editor(db, user_id).active_draft_id = draft.id
                return self._view(draft)
        try:
            _, raw_id, raw_revision, action = data.split(":", 3)
            draft_id, revision = str(UUID(hex=raw_id)), int(raw_revision)
        except (ValueError, TypeError):
            return View("Кнопка устарела.")
        with self.store.session() as db:
            draft = db.get(Draft, draft_id)
            if not draft or draft.user_id != user_id:
                return View("Черновик недоступен.")
            if draft.revision != revision:
                return View("Эта кнопка устарела. Откройте /drafts для текущего шага.")
            editor = self.store.editor(db, user_id)
            if draft.state != "published" and editor.active_draft_id != draft_id:
                return View("Это уже не активный черновик. Откройте /drafts.")
            site_id = draft.site_id
            state = draft.state
        if state == "site" and action.startswith("s") and action[1:].isdigit():
            index = int(action[1:])
            if index >= len(self.config.sites):
                return View("Сайт не найден.")
            site_id = self.config.sites[index].id
            try:
                snapshot = await self._snapshot(site_id)
            except SiteError as exc:
                return View(str(exc) + " Нажмите кнопку сайта ещё раз.")
            with self.store.session() as db, db.begin():
                draft = db.get(Draft, draft_id, with_for_update=True)
                if draft.revision != revision or draft.state != "site":
                    return View("Кнопка устарела. Откройте /drafts.")
                draft.site_id, draft.option_snapshot, draft.state = site_id, snapshot, "title"
                draft.deliveries.append(Delivery(site_id=site_id))
                draft.revision += 1
                return self._view(draft)
        if state == "edit_select" and action in {"ecategory", "eauthor"}:
            try:
                snapshot = await self._snapshot(site_id)
            except SiteError as exc:
                return View(str(exc) + " Повторите выбор поля.")
        else:
            snapshot = None
        if action in {"pub", "retry"}:
            with self.store.session() as db, db.begin():
                draft = db.get(Draft, draft_id, with_for_update=True)
                if draft.revision != revision or (draft.state, action) not in {("preview", "pub"), ("publishing", "retry"), ("failed", "retry")}:
                    return View("Кнопка устарела. Откройте /drafts.")
                if draft.state == "preview" and not (draft.title and draft.content and draft.category_slug):
                    return View("Заполните заголовок, текст и категорию.")
                draft.state = "publishing"
                draft.revision += 1
            return await self._publish(user_id, draft_id)
        if action == "cancel":
            return self.cancel(user_id, expected_draft_id=draft_id)
        with self.store.session() as db, db.begin():
            draft = db.get(Draft, draft_id, with_for_update=True)
            if draft.revision != revision or (draft.state != state):
                return View("Кнопка устарела. Откройте /drafts.")
            if action == "clone" and state in {"failed", "published"}:
                draft.revision += 1
                clone = self.store.new(db, user_id)
                clone.site_id, clone.title, clone.summary, clone.content = draft.site_id, draft.title, draft.summary, draft.content
                clone.category_slug, clone.author_slug, clone.tag_names = draft.category_slug, draft.author_slug, list(draft.tag_names)
                clone.option_snapshot = dict(draft.option_snapshot)
                clone.state = "preview"
                clone.deliveries.append(Delivery(site_id=draft.site_id))
                return View("Создана новая версия с новым ID. Это будет новая статья; фото нужно выбрать заново.\n\n" + self._view(clone).text, self._view(clone).buttons)
            if state == "summary" and action == "skip":
                draft.summary = ""
                self._advance(draft, "content")
            elif state == "content" and action == "done":
                if not draft.content.strip():
                    return View("Добавьте основной текст перед продолжением.")
                self._advance(draft, "photo")
            elif state == "photo" and action == "skip":
                draft.photo_file_id = None
                draft.photo_suffix = None
                self._advance(draft, "category")
            elif state in {"category", "author"} and action[:1] == ("c" if state == "category" else "a") and action[1:].isdigit():
                key = "categories" if state == "category" else "authors"
                options = draft.option_snapshot.get(key, [])
                index = int(action[1:])
                if index >= len(options):
                    return View("Вариант устарел. Откройте /drafts.")
                setattr(draft, "category_slug" if state == "category" else "author_slug", options[index]["slug"])
                self._advance(draft, "author" if state == "category" else "tags")
            elif state == "author" and action == "skip":
                draft.author_slug = None
                self._advance(draft, "tags")
            elif state == "tags" and action == "skip":
                draft.tag_names = []
                self._advance(draft, "preview")
            elif state == "preview" and action == "edit":
                draft.state = "edit_select"
            elif state == "edit_select" and action == "back":
                draft.state = "preview"
            elif state == "edit_select" and action.startswith("e") and action[1:] in {"title", "summary", "content", "photo", "category", "author", "tags"}:
                field = action[1:]
                draft.edit_field = field
                draft.state = field
                if field == "content":
                    draft.content = ""
                if snapshot is not None:
                    draft.option_snapshot = snapshot
            else:
                return View("Кнопка не подходит к текущему шагу. Откройте /drafts.")
            draft.revision += 1
            return self._view(draft)

    async def _publish(self, user_id: int, draft_id: str) -> View:
        with self.store.session() as db, db.begin():
            draft = db.get(Draft, draft_id)
            delivery = draft.deliveries[0]
            if delivery.lease_until > int(time.time()):
                return View("Публикация уже выполняется. Подождите и проверьте /drafts.")
            delivery.lease_until = int(time.time()) + 90
            external_id, site_id = delivery.external_id, delivery.site_id
            photo = (draft.photo_file_id, draft.photo_suffix) if draft.photo_file_id else None
            payload = {"title": draft.title, "content": draft.content, "summary": draft.summary,
                       "category_slugs": [draft.category_slug], "author_slug": draft.author_slug, "tag_names": list(draft.tag_names)}
        publisher = self.publishers[site_id]
        try:
            for attempt in range(2):
                try:
                    article = await self._send_to_site(publisher, draft_id, external_id, payload, photo)
                    break
                except SiteError as exc:
                    if not exc.retryable or attempt == 1:
                        raise
            if article.status != "published" or not article.url:
                raise SiteError("Сайт не подтвердил публикацию. Повторите действие.", retryable=True)
            self._save_delivery(draft_id, article, "published")
            with self.store.session() as db, db.begin():
                draft = db.get(Draft, draft_id, with_for_update=True)
                draft.state = "published"
                draft.revision += 1
                draft.photo_file_id = None
                draft.photo_suffix = None
                draft.deliveries[0].lease_until = 0
                editor = self.store.editor(db, user_id)
                if editor.active_draft_id == draft_id:
                    editor.active_draft_id = None
            return View(f"Статья опубликована: {article.url}")
        except SiteError as exc:
            log.warning("Site publish failed: site=%s draft=%s status=%s retryable=%s", site_id, draft_id, exc.status, exc.retryable)
            with self.store.session() as db, db.begin():
                draft = db.get(Draft, draft_id, with_for_update=True)
                if draft.state == "publishing":
                    draft.state = "failed"
                    draft.revision += 1
                draft.deliveries[0].lease_until = 0
                view = self._view(draft)
            return View(str(exc) + "\n\n" + view.text, view.buttons)

    async def _send_to_site(self, publisher: SitePublisher, draft_id: str, external_id: str, payload: dict, photo: tuple[str, str] | None):
        article = await publisher.get_article(external_id)
        if article is None or article.status != "published":
            article = await publisher.create_article(external_id, payload)
            self._save_delivery(draft_id, article, "created")
            if photo and not article.image_url:
                image_bytes = await self._load_photo(photo[0], photo[1])
                article = await publisher.upload_image(external_id, "telegram" + photo[1], image_bytes)
                if not article.image_url:
                    raise SiteError("Сайт не подтвердил загрузку фото. Повторите действие.", retryable=True)
                self._save_delivery(draft_id, article, "image_uploaded")
            article = await publisher.publish_article(external_id)
        return article

    async def _load_photo(self, file_id: str, suffix: str) -> bytes:
        if self.photo_loader is None:
            raise SiteError("Загрузка фото из Telegram не настроена.")
        try:
            data = await self.photo_loader(file_id)
        except Exception as exc:
            log.warning("Telegram photo retrieval failed: %s", type(exc).__name__)
            raise SiteError("Не удалось получить фото из Telegram. Повторите действие.", retryable=True) from exc
        signature = data[:12]
        valid = (suffix == ".jpg" and signature.startswith(b"\xff\xd8\xff")) or (suffix == ".png" and signature.startswith(b"\x89PNG\r\n\x1a\n")) or (suffix == ".webp" and signature.startswith(b"RIFF") and signature[8:12] == b"WEBP")
        if len(data) > 10 * 1024 * 1024 or not valid:
            raise SiteError("Фото должно быть JPG, PNG или WebP размером до 10 МБ.")
        return data

    def _save_delivery(self, draft_id: str, article, stage: str) -> None:
        with self.store.session() as db, db.begin():
            delivery = db.scalar(select(Delivery).where(Delivery.draft_id == draft_id).with_for_update())
            delivery.stage = stage
            delivery.article_id = article.article_id
            delivery.url = article.url
            delivery.image_url = article.image_url
            if stage == "published":
                delivery.published_at = utcnow()
