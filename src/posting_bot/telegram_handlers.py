from __future__ import annotations

import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .workflow import BotService, PhotoRef, View

log = logging.getLogger(__name__)
MENU = ReplyKeyboardMarkup([["Создать статью", "Черновики"], ["Отмена"]], resize_keyboard=True)
MAX_PHOTO = 10 * 1024 * 1024


def keyboard(view: View) -> InlineKeyboardMarkup | None:
    if not view.buttons:
        return None
    return InlineKeyboardMarkup([[InlineKeyboardButton(label, callback_data=data) for label, data in r] for r in view.buttons])


async def send(update: Update, view: View, *, menu: bool = False) -> None:
    message = update.effective_message
    if message is None:
        return
    await message.reply_text(view.text, reply_markup=MENU if menu else keyboard(view), disable_web_page_preview=True)


def private_user(update: Update, service: BotService) -> int | None:
    user = update.effective_user
    chat = update.effective_chat
    if chat is None or chat.type != "private" or user is None or not service.allowed(user.id):
        return None
    return user.id


def register(app: Application, service: BotService) -> None:
    async def load_photo(file_id: str) -> bytes:
        tg_file = await app.bot.get_file(file_id)
        return bytes(await tg_file.download_as_bytearray())

    service.photo_loader = load_photo

    async def command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = private_user(update, service)
        if user_id is None:
            await send(update, View("Нет доступа к этому боту."))
            return
        cmd = update.message.text.split()[0].split("@")[0]
        if cmd == "/start":
            view = service.start(user_id)
        elif cmd == "/new":
            view = service.start(user_id, fresh=True)
        elif cmd == "/drafts":
            view = service.drafts(user_id)
        else:
            view = service.cancel(user_id)
        await send(update, view, menu=cmd == "/start" and not view.buttons)

    async def callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        user_id = private_user(update, service)
        if user_id is None:
            await query.answer("Нет доступа", show_alert=True)
            return
        await query.answer()
        await send(update, await service.callback(user_id, query.data or ""))

    async def message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = private_user(update, service)
        if user_id is None:
            await send(update, View("Нет доступа к этому боту."))
            return
        value = update.message.text
        if value == "Создать статью":
            view = service.start(user_id, fresh=True)
        elif value == "Черновики":
            view = service.drafts(user_id)
        elif value == "Отмена":
            view = service.cancel(user_id)
        else:
            view = await service.input(user_id, value, update_id=update.update_id)
        await send(update, view)

    async def photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = private_user(update, service)
        if user_id is None:
            await send(update, View("Нет доступа к этому боту."))
            return
        if service.active_state(user_id) != "photo":
            await send(update, View("Фото сейчас не ожидается. Откройте /drafts."))
            return
        msg = update.message
        if msg.photo:
            item = msg.photo[-1]
            file_id, size, suffix = item.file_id, item.file_size, ".jpg"
        elif msg.document and msg.document.mime_type in {"image/jpeg", "image/png", "image/webp"}:
            item = msg.document
            file_id, size = item.file_id, item.file_size
            suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[item.mime_type]
        else:
            await send(update, View("Отправьте JPG, PNG или WebP."))
            return
        if size and size > MAX_PHOTO:
            await send(update, View("Фото должно быть не больше 10 МБ."))
            return
        view = await service.input(user_id, PhotoRef(file_id, suffix), update_id=update.update_id)
        await send(update, view)

    app.add_handler(CommandHandler(["start", "new", "drafts", "cancel"], command))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.PHOTO | filters.Document.IMAGE), photo))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, message))

    async def error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Telegram update failed: %s", type(context.error).__name__)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("Произошла ошибка. Откройте /drafts и повторите действие.")

    app.add_error_handler(error)

