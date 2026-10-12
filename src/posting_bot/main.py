from __future__ import annotations

import logging

from telegram.ext import Application

from .config import load_config
from .publisher import FxlfmPublisher
from .storage import Store
from .telegram_handlers import register
from .workflow import BotService


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = load_config()
    store = Store(config.database_url)
    publishers = {}
    for site in config.sites:
        if site.adapter != "fxlfm":
            raise ValueError(f"Unsupported adapter: {site.adapter}")
        publishers[site.id] = FxlfmPublisher(site)
    service = BotService(config, store, publishers)

    async def shutdown(app: Application) -> None:
        for publisher in publishers.values():
            await publisher.close()

    app = Application.builder().token(config.telegram_token).concurrent_updates(False).post_shutdown(shutdown).build()
    register(app, service)
    app.run_polling(drop_pending_updates=False, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
