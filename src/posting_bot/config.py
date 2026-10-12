from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv


@dataclass(frozen=True)
class SiteConfig:
    id: str
    name: str
    api_base: str
    token: str
    adapter: str = "fxlfm"


@dataclass(frozen=True)
class Config:
    telegram_token: str
    allowed_user_ids: frozenset[int]
    database_url: str
    photo_dir: Path
    sites: tuple[SiteConfig, ...]


def load_config() -> Config:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token.startswith("replace-"):
        raise ValueError("Set TELEGRAM_BOT_TOKEN")
    try:
        users = frozenset(int(s.strip()) for s in os.environ["ALLOWED_USER_IDS"].split(",") if s.strip())
    except (KeyError, ValueError) as exc:
        raise ValueError("ALLOWED_USER_IDS must be comma-separated Telegram user IDs") from exc
    if not users:
        raise ValueError("ALLOWED_USER_IDS cannot be empty")
    specs = [{"id": "fxlfm", "name": "fxlfm", "api_base": os.environ.get("FXLFM_API_BASE", "http://127.0.0.1:8000/api/bot/v1/"), "token_env": "FXLFM_INTEGRATION_TOKEN"}]
    specs += json.loads(os.environ.get("SITES_JSON", "[]"))
    sites = []
    for spec in specs:
        secret = os.environ.get(spec["token_env"], "").strip()
        if not secret or secret.startswith("replace-"):
            raise ValueError(f"Set {spec['token_env']}")
        base = spec["api_base"].rstrip("/") + "/"
        parsed = urlparse(base)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError(f"Invalid API URL for {spec['id']}")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1", "db", "backend", "host.docker.internal"}:
            raise ValueError("HTTP site API is allowed only for local development")
        sites.append(SiteConfig(spec["id"], spec["name"], base, secret, spec.get("adapter", "fxlfm")))
    if len({s.id for s in sites}) != len(sites):
        raise ValueError("Site IDs must be unique")
    return Config(token, users, os.environ.get("DATABASE_URL", "sqlite:///./bot.sqlite3"), Path(os.environ.get("PHOTO_DIR", "./photo_cache")), tuple(sites))

