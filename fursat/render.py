"""Сборка текста поста в фирменном формате (HTML для Telegram)."""
from __future__ import annotations

from html import escape

from . import config

BANNED = ("гарантированн", "100% доход", "без вложений и риска", "пассивный доход без")


def render_post(rubric: str, title: str, lead: str, facts: list[str] | None = None,
                impact_heading: str = "", impact: str = "", tip: str = "",
                source_name: str = "", source_url: str = "", extra_sources: list[dict] | None = None) -> str:
    emoji = config.RUBRICS.get(rubric, config.RUBRICS["tech"])["emoji"]
    parts = [f"{emoji} <b>{escape(title)}</b>", escape(lead)]
    if facts:
        parts.append("\n".join(f"— {escape(f)}" for f in facts))
    if impact:
        heading = f"<b>{escape(impact_heading)}</b>\n" if impact_heading else ""
        parts.append(heading + escape(impact))
    if tip:
        parts.append(f"💡 {escape(tip)}")
    footer = []
    if source_url:
        links = [f'<a href="{escape(source_url, quote=True)}">{escape(source_name or "первоисточник")}</a>']
        for s in extra_sources or []:
            links.append(f'<a href="{escape(s["url"], quote=True)}">{escape(s["name"])}</a>')
        footer.append("Источник: " + ", ".join(links))
    footer.append("@" + config.BRAND["username"])
    parts.append("\n".join(footer))
    return "\n\n".join(parts)


def fit_caption(fields: dict, limit: int) -> str:
    """Ужимает пост под лимит подписи к фото: сначала убирает совет, затем лишние факты."""
    text = render_post(**fields)
    if len(visible(text)) <= limit:
        return text
    trimmed = dict(fields, tip="")
    text = render_post(**trimmed)
    facts = list(trimmed.get("facts") or [])
    while len(visible(text)) > limit and len(facts) > 2:
        facts.pop()
        trimmed["facts"] = facts
        text = render_post(**trimmed)
    return text  # если всё ещё длинно — пост выйдет текстом без картинки (лимит 4096)


def visible(html_text: str) -> str:
    """Текст без тегов — именно его длину считает Telegram."""
    import re
    from html import unescape
    return unescape(re.sub(r"<[^>]+>", "", html_text))


def has_banned(text: str) -> str | None:
    low = text.lower()
    return next((b for b in BANNED if b in low), None)
