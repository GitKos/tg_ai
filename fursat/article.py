"""Загрузка полного текста статьи-первоисточника для проверки фактов."""
from __future__ import annotations

import re

import requests
from bs4 import BeautifulSoup

from . import config

MAX_CHARS = 12000  # редактору передаём начало статьи; об обрезке ему сообщается явно


def fetch_article(url: str) -> tuple[str, bool]:
    """Возвращает (текст, обрезан_ли). Пустая строка — если текст получить не удалось."""
    try:
        resp = requests.get(url, headers={"User-Agent": config.USER_AGENT, "Accept-Language": "ru,en;q=0.8"},
                            timeout=config.SETTINGS["collect"]["request_timeout"])
        resp.raise_for_status()
    except requests.RequestException:
        return "", False
    soup = BeautifulSoup(resp.content, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form", "figure", "noscript"]):
        tag.decompose()
    root = soup.find("article") or soup.find("main") or soup.body or soup
    parts = []
    for el in root.find_all(["p", "h2", "h3", "li", "blockquote"]):
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True))
        if len(text) >= 40 or (el.name in ("h2", "h3") and text):
            parts.append(text)
    text = "\n".join(dict.fromkeys(parts))            # убираем повторяющиеся абзацы
    if len(text) < 300:                               # paywall или страница на JS
        return "", False
    return text[:MAX_CHARS], len(text) > MAX_CHARS
