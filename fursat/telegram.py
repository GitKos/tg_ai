"""Тонкая обёртка над Telegram Bot API (публикация, оформление канала, команды администратора)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import requests

from . import config


class TelegramError(RuntimeError):
    pass


def call(method: str, files: dict | None = None, **params):
    if not config.BOT_TOKEN:
        raise TelegramError("TELEGRAM_BOT_TOKEN не задан")
    url = f"https://api.telegram.org/bot{config.BOT_TOKEN}/{method}"
    data = {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
            for k, v in params.items() if v is not None}
    for attempt in range(4):
        try:
            resp = requests.post(url, data=data, files=files, timeout=60)
            payload = resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == 3:
                raise TelegramError(f"{method}: сеть недоступна ({type(exc).__name__})") from None
            time.sleep(2 * (attempt + 1))
            continue
        if payload.get("ok"):
            return payload["result"]
        retry_after = payload.get("parameters", {}).get("retry_after")
        if retry_after and attempt < 3:            # лимит запросов — ждём, сколько просит Telegram
            time.sleep(min(int(retry_after), 60) + 1)
            continue
        raise TelegramError(f"{method}: {payload.get('error_code')} {payload.get('description')}")
    raise TelegramError(f"{method}: превышено число попыток")


def send_post(text: str, image: Path | None = None, chat: str | None = None) -> int:
    """Публикует пост. С картинкой — если текст укладывается в лимит подписи (1024 символа)."""
    chat = chat or config.CHANNEL
    if image and Path(image).exists() and len(text) <= 1024:
        with open(image, "rb") as fh:
            msg = call("sendPhoto", files={"photo": fh}, chat_id=chat, caption=text, parse_mode="HTML")
    else:
        msg = call("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
                   link_preview_options={"is_disabled": True})
    return msg["message_id"]


def send_poll(question: str, options: list[str], chat: str | None = None) -> int:
    msg = call("sendPoll", chat_id=chat or config.CHANNEL, question=question,
               options=[{"text": o} for o in options], is_anonymous=True)
    return msg["message_id"]


def notify_admin(text: str) -> None:
    """Сообщение владельцу. Ошибка уведомления не должна ронять конвейер."""
    if not (config.BOT_TOKEN and config.ADMIN_CHAT):
        return
    try:
        call("sendMessage", chat_id=config.ADMIN_CHAT, text=text[:4000], parse_mode="HTML",
             link_preview_options={"is_disabled": True})
    except TelegramError as exc:
        print(f"[WARN] не удалось уведомить администратора: {exc}")


def subscribers() -> int:
    return int(call("getChatMemberCount", chat_id=config.CHANNEL))
