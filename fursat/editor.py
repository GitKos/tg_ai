"""AI-редактор: проверка фактов по первоисточнику, отбор, авторский пересказ и оформление.

Каждая новость-кандидат проходит: загрузка полного текста → решение редактора (Claude, структурированный
ответ) → программные проверки достоверности → готовый пост в очереди.
"""
from __future__ import annotations

import json
from datetime import timedelta
from typing import Literal

import anthropic
from pydantic import BaseModel, Field, ValidationError

from . import config, db, images, render
from .article import fetch_article

Rubric = Literal["ai", "money", "business", "tech", "trends", "uzbekistan"]


class Decision(BaseModel):
    publish: bool = Field(description="Публиковать ли новость в канале")
    reject_reason: str = Field(description="Если publish=false — краткая причина; иначе пустая строка")
    rubric: Rubric
    title: str = Field(description="Заголовок до 80 знаков: сильный, конкретный, без кликбейта и обмана")
    lead: str = Field(description="1–2 предложения: что произошло, кто и когда")
    facts: list[str] = Field(description="2–4 ключевых факта из статьи, каждый до 110 знаков")
    impact_heading: str = Field(description="Подзаголовок блока пользы, напр. 'Что это значит для заработка?'")
    impact: str = Field(description="2–3 предложения: чем новость важна человеку из Узбекистана")
    tip: str = Field(description="Практический совет одним предложением или пустая строка, если он неуместен")
    event_date: str = Field(description="Дата события YYYY-MM-DD, если указана в статье, иначе пустая строка")
    credibility: int = Field(description="Достоверность 0–100: насколько факты подтверждены текстом источника")
    virality: int = Field(description="Интерес для аудитории канала 0–100")
    is_rumor: bool = Field(description="true, если это слух, утечка, прогноз СМИ без официального подтверждения")
    sensitive: bool = Field(description="true, если тема — финансы, инвестиции, налоги или законы Узбекистана")


SYSTEM = """Ты — главный редактор Telegram-канала «{name}» для жителей Узбекистана.
Тематика: искусственный интеллект, заработок в интернете, бизнес и предпринимательство, технологии, \
тренды, возможности в Узбекистане. Читатель — обычный человек 20–40 лет: предприниматель, фрилансер, \
специалист, студент. Он хочет понять, как новость повлияет на его доход, работу или бизнес.

Тебе дают материал из источника. Реши, достоин ли он публикации, и если да — напиши авторский пересказ \
на языке: {language}.

Правила достоверности (нарушать нельзя):
- Опирайся только на текст источника. Не добавляй цифры, цитаты, даты и детали, которых в нём нет.
- Не выдавай предположения за факты. Слухи, утечки, «по данным СМИ», неподтверждённые планы — is_rumor=true.
- Не обещай заработок и не придумывай суммы доходов. Блок пользы — это возможности и выводы, а не обещания.
- Не копируй предложения источника дословно: пересказывай своими словами, добавляй контекст и объяснение.
- Если тема касается финансов, инвестиций, налогов или законодательства Узбекистана — sensitive=true и \
особая осторожность в формулировках: что именно решено, кем, с какого срока.

Отклоняй (publish=false): политику, криминал, происшествия, спорт, светскую хронику, рекламные \
пресс-релизы без пользы, узкие корпоративные новости, всё, что уже публиковалось (список ниже), и новости, \
которые нельзя связать с жизнью, работой или бизнесом читателя в Узбекистане.

Стиль: живой и ясный русский язык, короткие предложения, без канцелярита и восторженных штампов. \
Заголовок цепляет фактом, а не преувеличением. Весь пост должен уложиться примерно в 800 знаков, \
поэтому пиши плотно. Подзаголовок блока пользы меняй по смыслу («Что это значит для бизнеса?», \
«Где применить», «Почему это важно», «Что делать»)."""


def _schema() -> dict:
    schema = Decision.model_json_schema()
    schema["additionalProperties"] = False
    return schema


def _ask(client: anthropic.Anthropic, con, item, text: str, truncated: bool, recent: list[str]) -> Decision | None:
    language = "русский" if config.BRAND["language"] == "ru" else "узбекский (латиница)"
    source_block = (text + ("\n[Текст статьи обрезан: показано только начало.]" if truncated else "")) if text \
        else f"[Полный текст недоступен. Есть только анонс из ленты:]\n{item['summary']}"
    user = (f"Источник: {item['source']} (уровень доверия {item['source_trust']} из 3)\n"
            f"Ссылка: {item['url']}\nДата публикации: {item['published_at']}\n"
            f"Другие источники, написавшие о том же: {item['confirmations']}\n"
            f"Заголовок: {item['title']}\n\nТекст:\n{source_block}\n\n"
            f"Уже опубликовано в канале за последние дни (не повторяй):\n" + "\n".join(f"- {t}" for t in recent))
    response = client.messages.create(
        model=config.LLM_MODEL,
        max_tokens=16000,
        system=SYSTEM.format(name=config.BRAND["name"], language=language),
        messages=[{"role": "user", "content": user}],
        output_config={"effort": config.SETTINGS["editor"]["effort"],
                       "format": {"type": "json_schema", "schema": _schema()}},
    )
    con.execute("INSERT INTO llm_usage VALUES(?,?,?,?)",
                (config.iso(), response.model, response.usage.input_tokens, response.usage.output_tokens))
    if response.stop_reason == "refusal":           # модель отказалась — новость просто пропускаем
        return None
    raw = next((b.text for b in response.content if b.type == "text"), "")
    try:
        return Decision.model_validate(json.loads(raw))
    except (ValidationError, ValueError) as exc:
        db.log_error(con, "editor:parse", f"item {item['id']}: {exc}")
        return None


def _verify(item, decision: Decision, has_text: bool) -> tuple[bool, str, str]:
    """Программные правила достоверности поверх решения модели. → (ok, verify_status, note)."""
    confirmed = item["confirmations"] >= 1
    if not decision.publish:
        return False, "rejected", decision.reject_reason or "отклонено редактором"
    if decision.is_rumor:
        return False, "rumor", "слух или неподтверждённая информация"
    if not has_text and item["source_trust"] < 3 and not confirmed:
        return False, "single_source", "нет полного текста и нет подтверждения из других источников"
    if item["source_trust"] == 1 and not confirmed:
        return False, "single_source", "агрегатор без подтверждения авторитетным источником"
    if decision.credibility < 75:
        return False, "rejected", f"низкая достоверность ({decision.credibility})"
    if decision.sensitive and item["source_trust"] < 3 and not confirmed:
        return False, "single_source", "финансы/законы Узбекистана: нужен первоисточник или второе подтверждение"
    if decision.virality < 55:
        return False, "rejected", f"недостаточно интересно ({decision.virality})"
    status = "verified" if (confirmed or item["source_trust"] == 3) else "single_source"
    return True, status, f"достоверность {decision.credibility}, интерес {decision.virality}"


def run(con, limit: int | None = None) -> int:
    """Обрабатывает лучших кандидатов. Возвращает число подготовленных постов."""
    cfg = config.SETTINGS["editor"]
    if not config.has_llm():
        return 0
    day_ago = config.iso(config.now_utc() - timedelta(hours=24))
    used_today = con.execute("SELECT COUNT(*) FROM llm_usage WHERE ts > ?", (day_ago,)).fetchone()[0]
    queued = con.execute("SELECT COUNT(*) FROM posts WHERE status='queued' AND origin IN ('auto','urgent')").fetchone()[0]
    budget = min(limit or cfg["candidates_per_run"], cfg["daily_limit"] - used_today,
                 max(0, cfg["queue_target"] - queued) + 1)
    if budget <= 0:
        return 0
    fresh = config.iso(config.now_utc() - timedelta(hours=config.SETTINGS["collect"]["freshness_hours"]))
    candidates = con.execute(
        """SELECT * FROM items WHERE status='new' AND verify_status != 'rumor' AND score >= ?
           AND COALESCE(published_at, fetched_at) > ? ORDER BY score DESC LIMIT ?""",
        (cfg["min_prescore"], fresh, budget)).fetchall()
    recent = [r["title"] for r in con.execute(
        "SELECT title FROM posts WHERE status IN ('queued','published') ORDER BY id DESC LIMIT 40")]
    client = anthropic.Anthropic()
    made = 0
    for item in candidates:
        try:
            text, truncated = fetch_article(item["url"])
            decision = _ask(client, con, item, text, truncated, recent)
        except anthropic.RateLimitError as exc:
            db.log_error(con, "editor:rate_limit", exc)
            break
        except anthropic.APIStatusError as exc:
            db.log_error(con, "editor:api", exc)
            if exc.status_code in (401, 403):       # неверный ключ — дальше пробовать бессмысленно
                break
            continue
        except anthropic.APIConnectionError as exc:
            db.log_error(con, "editor:network", exc)
            break
        if decision is None:
            con.execute("UPDATE items SET status='skipped', verify_note='нет решения редактора' WHERE id=?",
                        (item["id"],))
            con.commit()
            continue
        ok, verify_status, note = _verify(item, decision, bool(text))
        fields = dict(rubric=decision.rubric, title=decision.title.strip(), lead=decision.lead.strip(),
                      facts=[f.strip() for f in decision.facts[:4]], impact_heading=decision.impact_heading.strip(),
                      impact=decision.impact.strip(), tip=decision.tip.strip(),
                      source_name=item["source"], source_url=item["url"])
        body = render.fit_caption(fields, cfg["max_caption_chars"]) if ok else ""
        banned = render.has_banned(body) if ok else None
        if banned:
            ok, verify_status, note = False, "rejected", f"запрещённая формулировка: «{banned}»"
        con.execute("""UPDATE items SET status=?, verify_status=?, verify_note=?, credibility=?, event_date=?,
                       topic=?, score=? WHERE id=?""",
                    ("used" if ok else "rejected", verify_status, note, decision.credibility,
                     decision.event_date or None, decision.rubric,
                     round(item["score"] * 0.4 + decision.virality * 0.6, 1), item["id"]))
        if ok:
            image = images.make_cover(decision.title, decision.rubric, seed=item["url"])
            urgent = decision.virality >= config.SETTINGS["urgent"]["min_score"] and verify_status == "verified"
            con.execute("""INSERT INTO posts(item_id,origin,rubric,title,body,source_name,source_url,image_path,
                           created_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (item["id"], "urgent" if urgent else "auto", decision.rubric, decision.title, body,
                         item["source"], item["url"], str(image.relative_to(config.ROOT)), config.iso()))
            recent.insert(0, decision.title)
            made += 1
        con.commit()
    return made
