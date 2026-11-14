"""Настройки проекта: YAML-конфиги + секреты из переменных окружения (.env)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
IMAGES = DATA / "images"
BACKUPS = DATA / "backups"
DB_PATH = Path(os.environ.get("FURSAT_DB", DATA / "fursat.db"))
ASSETS = ROOT / "assets"


def _load_dotenv() -> None:
    """Минимальный загрузчик .env: секреты не хранятся в коде и не попадают в git."""
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

SETTINGS: dict = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
SOURCES: list[dict] = yaml.safe_load((ROOT / "config" / "sources.yaml").read_text(encoding="utf-8"))["sources"]

BRAND = SETTINGS["brand"]
RUBRICS: dict = SETTINGS["rubrics"]
TZ = timezone(timedelta(hours=SETTINGS["timezone_offset_hours"]))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.environ.get("TELEGRAM_CHANNEL", "")          # @username или числовой id канала
ADMIN_CHAT = os.environ.get("TELEGRAM_ADMIN_CHAT", "")     # личный chat id владельца (алерты, команды)
LLM_MODEL = os.environ.get("LLM_MODEL") or SETTINGS["editor"]["model"]
USER_AGENT = "Mozilla/5.0 (compatible; FursatNewsBot/1.0; +https://t.me/" + BRAND["username"] + ")"


def has_llm() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(TZ)


def iso(dt: datetime | None = None) -> str:
    return (dt or now_utc()).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


for _d in (DATA, IMAGES, BACKUPS):
    _d.mkdir(parents=True, exist_ok=True)
