"""Сбор новостей из RSS, защита от дублей и предварительная оценка интереса."""
from __future__ import annotations

import html
import re
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

from . import config, db

TRACKING = re.compile(r"^(utm_|fbclid|gclid|yclid|ref$|ref_|mc_|igshid|cmpid|ncid)", re.I)
TAGS = re.compile(r"<[^>]+>")
WORD = re.compile(r"[a-zа-яёўқғҳ0-9]+", re.I)
STOP = set("""the a an of to in on for and or is are was with from by at as its it this that new how why what
you can now will says said lets let get more than over after into about your has have been
и в на с по для из о об от до за что как это не к у а но или его ее их при же бы ли то во со
году года год после более узбекистан узбекистане узбекистана узбекистану ташкенте стран страны""".split())

# Ключевые слова → (вес, тема). Тема новости = тема с наибольшим суммарным весом.
KEYWORDS: dict[str, tuple[int, str]] = {
    # ИИ
    "ai": (9, "ai"), "artificial intelligence": (9, "ai"), "ии": (9, "ai"), "ии-": (9, "ai"),
    "нейросет": (10, "ai"),
    "chatgpt": (12, "ai"), "openai": (10, "ai"), "claude": (11, "ai"), "anthropic": (10, "ai"),
    "gemini": (10, "ai"), "llm": (7, "ai"), "copilot": (7, "ai"), "agent": (6, "ai"), "агент": (6, "ai"),
    "искусственн": (9, "ai"), "deepmind": (7, "ai"), "midjourney": (7, "ai"), "sora": (7, "ai"),
    "grok": (6, "ai"), "deepseek": (8, "ai"), "mistral": (5, "ai"), "perplexity": (6, "ai"),
    # заработок
    "earn": (8, "money"), "income": (7, "money"), "creator": (7, "money"), "monetiz": (9, "money"),
    "freelanc": (9, "money"), "фриланс": (9, "money"), "заработ": (10, "money"), "доход": (8, "money"),
    "side hustle": (10, "money"), "remote work": (8, "money"), "удаленн": (7, "money"),
    "youtube": (6, "money"), "tiktok": (6, "money"), "telegram": (8, "money"), "salary": (6, "money"),
    "зарплат": (7, "money"), "jobs": (5, "money"), "ваканс": (6, "money"), "монетизац": (9, "money"),
    # бизнес
    "startup": (7, "business"), "стартап": (8, "business"), "funding": (5, "business"),
    "raises": (4, "business"), "инвестиц": (6, "business"), "бизнес": (6, "business"),
    "предпринимат": (8, "business"), "small business": (8, "business"), "e-commerce": (6, "business"),
    "маркетплейс": (7, "business"), "грант": (9, "business"), "субсид": (8, "business"),
    "льгот": (8, "business"), "кредит": (5, "business"), "экспорт": (5, "business"), "налог": (7, "business"),
    # технологии
    "robot": (7, "tech"), "робот": (7, "tech"), "chip": (4, "tech"), "nvidia": (6, "tech"),
    "iphone": (6, "tech"), "samsung": (5, "tech"), "tesla": (5, "tech"), "starlink": (8, "tech"),
    "quantum": (5, "tech"), "automation": (7, "tech"), "автоматизац": (8, "tech"), "5g": (5, "tech"),
    # Узбекистан
    "uzbekistan": (14, "uzbekistan"), "узбекистан": (8, "uzbekistan"), "tashkent": (10, "uzbekistan"),
    "ташкент": (5, "uzbekistan"), "it park": (10, "uzbekistan"), "it-park": (10, "uzbekistan"),
    "uzum": (9, "uzbekistan"), "click": (3, "uzbekistan"), "payme": (8, "uzbekistan"),
    "central asia": (8, "uzbekistan"), "самозанят": (9, "uzbekistan"), "айти": (5, "uzbekistan"),
    # тренды
    "viral": (7, "trends"), "trend": (5, "trends"), "launch": (3, "trends"), "app": (2, "trends"),
    "free": (4, "trends"), "бесплатн": (6, "trends"),
}
# Маркеры слухов и неподтверждённой информации — такие новости не публикуются как факт.
RUMOR = re.compile(r"\b(rumou?r|reportedly|allegedly|leak(ed|s)?|unconfirmed|sources say|might|"
                   r"слух|якобы|по слухам|утечк|инсайд|возможно)\b", re.I)
# Нерелевантное для канала: политика, криминал, спорт, происшествия.
OFFTOPIC = re.compile(r"\b(убий|дтп|погиб|приговор|задержа|арест|насили|наркот|осуд|браконьер|футбол|матч|"
                      r"чемпионат|землетряс|пожар|концерт|потеплен|погод|инфекци|"
                      r"war|killed|shooting|election|murder|dies|death|coupon|promo code|deals)", re.I)


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not TRACKING.match(k)]
    host = parts.netloc.lower().removeprefix("www.")
    return urlunsplit((parts.scheme.lower() or "https", host, parts.path.rstrip("/"), urlencode(query), ""))


def clean(text: str | None) -> str:
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", text or ""))).strip()


def title_key(title: str) -> str:
    words = [w for w in WORD.findall(title.lower()) if w not in STOP and len(w) > 2]
    return " ".join(sorted(set(words)))


def similar(key_a: str, key_b: str) -> float:
    """Похожесть заголовков: максимум из коэффициента Жаккара и посимвольного сходства."""
    a, b = set(key_a.split()), set(key_b.split())
    if not a or not b:
        return 0.0
    common = len(a & b)
    jaccard = common / len(a | b)
    # разные издания формулируют одну новость по-разному: 3+ общих значимых слова,
    # составляющих половину более короткого заголовка, — тоже совпадение
    overlap = common / min(len(a), len(b)) if common >= 3 else 0.0
    return max(jaccard, overlap * 1.25, SequenceMatcher(None, key_a, key_b).ratio() * 0.9)


def prescore(title: str, summary: str, source: dict, published: datetime | None) -> tuple[float, str, bool]:
    """Эвристическая оценка 0..100, тема и признак слуха. Точную оценку потом даёт редактор."""
    text = f"{title} {title} {summary}".lower()          # заголовок весит вдвое
    topics: dict[str, int] = {}
    for kw, (weight, topic) in KEYWORDS.items():
        # короткие слова (ai, ии, app) ищем целиком, остальные — как начало слова (основу)
        pattern = rf"\b{re.escape(kw)}\b" if len(kw) <= 5 else rf"\b{re.escape(kw)}"
        if re.search(pattern, text):
            topics[topic] = topics.get(topic, 0) + weight
    local = source["region"] == "uz"
    if local:
        # для местных изданий слово «Узбекистан» есть в каждой новости — релевантность даёт только тема канала
        topics.pop("uzbekistan", None)
    relevance = min(sum(topics.values()), 45)
    topic = max(topics, key=topics.get) if topics else source.get("topic", "tech")
    score = relevance + source["trust"] * 6
    if local:
        score += 10 if topics else -25                   # местные новости нужны, но только по теме канала
    if published:
        age_h = (config.now_utc() - published).total_seconds() / 3600
        score += max(0.0, 15 - age_h * 0.5)             # свежесть
    is_rumor = bool(RUMOR.search(f"{title} {summary}"))
    if is_rumor:
        score -= 20
    if OFFTOPIC.search(title):
        score -= 40
    return max(0.0, min(100.0, score)), topic, is_rumor


def fetch_feed(source: dict) -> list[dict]:
    resp = requests.get(source["url"], headers={"User-Agent": config.USER_AGENT},
                        timeout=config.SETTINGS["collect"]["request_timeout"])
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    if feed.bozo and not feed.entries:
        raise ValueError(f"не удалось разобрать ленту: {feed.bozo_exception}")
    out = []
    for e in feed.entries[:40]:
        link, title = e.get("link"), clean(e.get("title"))
        if not link or not title:
            continue
        stamp = e.get("published_parsed") or e.get("updated_parsed")
        published = datetime(*stamp[:6], tzinfo=timezone.utc) if stamp else None  # feedparser отдаёт UTC
        out.append({"url": normalize_url(link), "title": title,
                    "summary": clean(e.get("summary"))[:1500], "published": published})
    return out


def collect(con) -> int:
    """Собирает ленты, сохраняет новые материалы, помечает дубли. Возвращает число новых записей."""
    settings = config.SETTINGS["collect"]
    cutoff = config.now_utc() - timedelta(hours=settings["freshness_hours"])
    recent = [dict(r) for r in con.execute(
        "SELECT id, title_key, source, status FROM items WHERE fetched_at > ?",
        (config.iso(config.now_utc() - timedelta(days=5)),))]
    added = 0
    for source in config.SOURCES:
        try:
            entries = fetch_feed(source)
            con.execute("""INSERT INTO source_health(source,last_ok,last_count,fails) VALUES(?,?,?,0)
                           ON CONFLICT(source) DO UPDATE SET last_ok=excluded.last_ok,
                           last_count=excluded.last_count, fails=0""",
                        (source["name"], config.iso(), len(entries)))
        except Exception as exc:  # один упавший источник не ломает сбор
            con.execute("""INSERT INTO source_health(source,last_error,fails) VALUES(?,?,1)
                           ON CONFLICT(source) DO UPDATE SET last_error=excluded.last_error, fails=fails+1""",
                        (source["name"], f"{config.iso()} {type(exc).__name__}: {exc}"[:300]))
            db.log_error(con, f"collect:{source['name']}", exc)
            continue
        for e in entries:
            if e["published"] and e["published"] < cutoff:
                continue
            if con.execute("SELECT 1 FROM items WHERE url=?", (e["url"],)).fetchone():
                continue
            key = title_key(e["title"])
            score, topic, is_rumor = prescore(e["title"], e["summary"], source, e["published"])
            dup = next((r for r in recent if similar(key, r["title_key"]) >= 0.62), None)
            status, dup_of = "new", None
            if dup:
                status, dup_of = "duplicate", dup["id"]
                if dup["source"] != source["name"]:       # та же новость в другом источнике = подтверждение
                    con.execute("UPDATE items SET confirmations=confirmations+1, "
                                "score=MIN(100, score+6) WHERE id=?", (dup["id"],))
            cur = con.execute(
                """INSERT INTO items(url,source,source_trust,region,title,title_key,summary,published_at,
                   fetched_at,topic,score,verify_status,status,dup_of) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (e["url"], source["name"], source["trust"], source["region"], e["title"], key, e["summary"],
                 config.iso(e["published"]) if e["published"] else None, config.iso(), topic, score,
                 "rumor" if is_rumor else "unverified", status, dup_of))
            if status == "new":
                recent.append({"id": cur.lastrowid, "title_key": key, "source": source["name"], "status": "new"})
                added += 1
        con.commit()
    db.kv_set(con, "last_collect", config.iso())
    return added


def check_sources() -> None:
    """Диагностика: какие ленты отвечают и сколько в них материалов."""
    for source in config.SOURCES:
        try:
            entries = fetch_feed(source)
            newest = max((e["published"] for e in entries if e["published"]), default=None)
            print(f"OK   {source['name']:<24} {len(entries):>3} записей, свежая: {newest:%d.%m %H:%M}" if newest
                  else f"OK   {source['name']:<24} {len(entries):>3} записей")
        except Exception as exc:
            print(f"FAIL {source['name']:<24} {type(exc).__name__}: {str(exc)[:90]}")
