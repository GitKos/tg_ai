"""Аналитика: подписчики (Bot API), просмотры и реакции (публичная страница t.me/s/<канал>),
еженедельный отчёт с выводами о рубриках, заголовках и времени публикаций."""
from __future__ import annotations

import re
from datetime import timedelta

import requests
from bs4 import BeautifulSoup

from . import config, db, telegram


def _number(text: str) -> int:
    """'1.2K' → 1200, '3M' → 3000000."""
    m = re.match(r"([\d.,]+)\s*([KkMm]?)", text.strip())
    if not m:
        return 0
    value = float(m.group(1).replace(",", "."))
    return int(value * {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1))


def fetch_views(username: str) -> dict[int, tuple[int, int]]:
    """Читает публичную веб-версию канала. → {message_id: (просмотры, реакции)}."""
    resp = requests.get(f"https://t.me/s/{username.lstrip('@')}", headers={"User-Agent": config.USER_AGENT}, timeout=20)
    resp.raise_for_status()
    out = {}
    for msg in BeautifulSoup(resp.text, "html.parser").select(".tgme_widget_message[data-post]"):
        message_id = int(msg["data-post"].split("/")[-1])
        views = msg.select_one(".tgme_widget_message_views")
        reactions = sum(_number(r.get_text(" ", strip=True).split()[-1])
                        for r in msg.select(".tgme_reaction") if r.get_text(strip=True))
        out[message_id] = (_number(views.get_text()) if views else 0, reactions)
    return out


def snapshot(con) -> None:
    """Раз в час снимает показатели канала и последних постов."""
    if not (config.BOT_TOKEN and config.CHANNEL):
        return
    last = db.kv_get(con, "last_snapshot")
    if last and config.now_utc() - config.parse_iso(last) < timedelta(minutes=55):
        return
    con.execute("INSERT INTO channel_stats VALUES(?,?)", (config.iso(), telegram.subscribers()))
    if config.CHANNEL.startswith("@"):               # просмотры доступны только у публичного канала
        for message_id, (views, reactions) in fetch_views(config.CHANNEL).items():
            row = con.execute("SELECT id FROM posts WHERE tg_message_id=?", (message_id,)).fetchone()
            if row:
                con.execute("UPDATE posts SET views=?, reactions=? WHERE id=?", (views, reactions, row["id"]))
                con.execute("INSERT INTO metrics VALUES(?,?,?,?)", (row["id"], config.iso(), views, reactions))
    db.kv_set(con, "last_snapshot", config.iso())
    con.commit()


def summary(con, days: int = 7) -> dict:
    since = config.iso(config.now_utc() - timedelta(days=days))
    subs = con.execute("SELECT subscribers FROM channel_stats ORDER BY ts DESC LIMIT 1").fetchone()
    subs_before = con.execute("SELECT subscribers FROM channel_stats WHERE ts <= ? ORDER BY ts DESC LIMIT 1",
                              (since,)).fetchone() or \
        con.execute("SELECT subscribers FROM channel_stats ORDER BY ts LIMIT 1").fetchone()
    posts = con.execute("SELECT * FROM posts WHERE status='published' AND published_at >= ?", (since,)).fetchall()
    viewed = [p for p in posts if p["views"]]
    avg_views = sum(p["views"] for p in viewed) / len(viewed) if viewed else 0
    by_rubric: dict[str, list[int]] = {}
    by_hour: dict[str, list[int]] = {}
    for p in viewed:
        by_rubric.setdefault(p["rubric"], []).append(p["views"])
        if p["slot"] and p["slot"] != "urgent":
            by_hour.setdefault(p["slot"][-5:], []).append(p["views"])
    avg = lambda d: sorted(((k, sum(v) / len(v), len(v)) for k, v in d.items()), key=lambda x: -x[1])
    usage = con.execute("SELECT COALESCE(SUM(input_tokens),0) i, COALESCE(SUM(output_tokens),0) o "
                        "FROM llm_usage WHERE ts >= ?", (since,)).fetchone()
    money = {r["kind"]: r["s"] for r in con.execute(
        "SELECT kind, SUM(amount_usd) s FROM finance WHERE ts >= ? GROUP BY kind", (since,))}
    subscribers = subs["subscribers"] if subs else None
    return {
        "days": days, "subscribers": subscribers,
        "growth": (subscribers - subs_before["subscribers"]) if subs and subs_before else None,
        "posts": len(posts), "avg_views": round(avg_views),
        # ERR — доля подписчиков, увидевших средний пост
        "err": round(100 * avg_views / subscribers, 1) if subscribers and avg_views else None,
        "reactions": sum(p["reactions"] or 0 for p in posts),
        "by_rubric": avg(by_rubric), "by_slot": avg(by_hour),
        "top": sorted(viewed, key=lambda p: -p["views"])[:5],
        "llm_tokens": (usage["i"], usage["o"]),
        "income": money.get("income", 0.0), "expense": money.get("expense", 0.0),
    }


def report_text(con, days: int = 7) -> str:
    s = summary(con, days)
    label = lambda r: config.RUBRICS.get(r, {}).get("label", r)
    lines = [f"📊 <b>{config.BRAND['name']}: отчёт за {days} дн.</b>",
             f"Подписчики: {s['subscribers'] if s['subscribers'] is not None else '—'}"
             + (f" ({s['growth']:+d})" if s["growth"] is not None else ""),
             f"Публикаций: {s['posts']}, средний охват: {s['avg_views']}, ERR: {s['err'] or '—'}%",
             f"Реакций: {s['reactions']}"]
    if s["by_rubric"]:
        lines.append("\n<b>Рубрики по охвату</b>")
        lines += [f"{label(r)}: {int(v)} (постов: {n})" for r, v, n in s["by_rubric"]]
    if s["by_slot"]:
        lines.append("\n<b>Время публикации по охвату</b>")
        lines += [f"{slot}: {int(v)} (постов: {n})" for slot, v, n in s["by_slot"]]
    if s["top"]:
        lines.append("\n<b>Лучшие заголовки</b>")
        lines += [f"{p['views']} — {p['title']}" for p in s["top"]]
    # выводы делаем только при достаточной выборке — одна случайная публикация стратегию не меняет
    solid = [(r, v, n) for r, v, n in s["by_rubric"] if n >= 4]
    if len(solid) >= 2:
        best, worst = solid[0], solid[-1]
        lines.append(f"\n<b>Вывод</b>\nСильнейшая рубрика — {label(best[0])}, слабейшая — {label(worst[0])}."
                     + (" Разрыв больше 1,5× — стоит перераспределить слоты." if best[1] > worst[1] * 1.5 else
                        " Разрыв небольшой — структуру не меняем."))
    else:
        lines.append("\nДанных для выводов по рубрикам пока мало (нужно ≥4 постов в рубрике).")
    lines.append(f"\nLLM-токены: вход {s['llm_tokens'][0]}, выход {s['llm_tokens'][1]}. "
                 f"Доход: ${s['income']:.0f}, расход: ${s['expense']:.0f}")
    return "\n".join(lines)


def weekly_report(con) -> None:
    """По понедельникам утром отправляет владельцу отчёт и сохраняет его в базе."""
    now = config.now_local()
    week = now.strftime("%G-W%V")
    if now.weekday() != 0 or now.hour < 9 or db.kv_get(con, "last_weekly") == week:
        return
    text = report_text(con, 7)
    db.kv_set(con, f"report:{week}", text)
    db.kv_set(con, "last_weekly", week)
    con.commit()
    telegram.notify_admin(text)
