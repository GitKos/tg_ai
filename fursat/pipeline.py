"""Конвейер: один «тик» = сбор → редактор → публикация по расписанию → аналитика → панель.

Тик идемпотентен: его можно запускать как угодно часто (cron GitHub Actions, VPS, вручную) —
слот публикации закрывается ровно один раз, а дубли отсекаются на уровне базы.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

from . import analytics, collect, config, dashboard, db, editor, images, render, telegram


# ── Стартовый контент ────────────────────────────────────────────────────────
def seed(con) -> int:
    """Загружает подготовленные публикации из content/starter_posts.json (повторно не добавляет)."""
    path = config.ROOT / "content" / "starter_posts.json"
    added = 0
    today = config.now_local().strftime("%Y-%m-%d")
    for p in json.loads(path.read_text(encoding="utf-8")):
        if con.execute("SELECT 1 FROM posts WHERE slug=?", (p["slug"],)).fetchone():
            continue
        if p.get("expires") and p["expires"] < today:   # новость устарела до запуска — не берём
            continue
        if p.get("kind") == "poll":
            body, options = p["question"], json.dumps(p["options"], ensure_ascii=False)
        else:
            options = None
            body = render.fit_caption(
                dict(rubric=p["rubric"], title=p["title"], lead=p["lead"], facts=p.get("facts"),
                     impact_heading=p.get("impact_heading", ""), impact=p.get("impact", ""), tip=p.get("tip", ""),
                     source_name=p.get("source_name", ""), source_url=p.get("source_url", ""),
                     extra_sources=p.get("extra_sources")),
                config.SETTINGS["editor"]["max_caption_chars"])
        con.execute("""INSERT INTO posts(slug,origin,rubric,title,body,kind,poll_options,source_name,source_url,
                       created_at,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (p["slug"], "starter", p["rubric"], p.get("title") or p["question"], body,
                     p.get("kind", "post"), options, p.get("source_name"), p.get("source_url"), config.iso(),
                     p.get("expires")))
        added += 1
    con.commit()
    return added


# ── Публикация ───────────────────────────────────────────────────────────────
def _cover(post) -> Path | None:
    if post["kind"] != "post":
        return None
    if post["image_path"] and (config.ROOT / post["image_path"]).exists():
        return config.ROOT / post["image_path"]
    # картинка детерминирована: при необходимости воссоздаём её из заголовка
    return images.make_cover(post["title"], post["rubric"], seed=post["source_url"] or post["slug"] or post["title"])


def publish(con, post, slot: str, dry_run: bool = False) -> bool:
    """Публикует пост и фиксирует результат. Ошибка помечает пост failed, но не роняет тик."""
    if dry_run:
        print(f"[DRY-RUN] слот {slot}: #{post['id']} [{post['rubric']}] {post['title']}")
        return True
    try:
        if post["kind"] == "poll":
            message_id = telegram.send_poll(post["body"], json.loads(post["poll_options"]))
        else:
            message_id = telegram.send_post(post["body"], _cover(post))
    except telegram.TelegramError as exc:
        db.log_error(con, "publish", f"post {post['id']}: {exc}")
        con.execute("UPDATE posts SET status='failed' WHERE id=?", (post["id"],))
        con.commit()
        telegram.notify_admin(f"⚠️ Не удалось опубликовать пост #{post['id']}: {exc}")
        return False
    con.execute("UPDATE posts SET status='published', slot=?, published_at=?, tg_message_id=? WHERE id=?",
                (slot, config.iso(), message_id, post["id"]))
    con.commit()
    print(f"[OK] опубликован #{post['id']} в слот {slot}: {post['title']}")
    return True


def _next_post(con, last_rubric: str | None):
    """Очередь: срочные → свежие новости редактора → стартовые. Рубрики стараемся чередовать."""
    rows = con.execute("""SELECT * FROM posts WHERE status='queued'
        ORDER BY CASE origin WHEN 'urgent' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END, id""").fetchall()
    if not rows:
        return None
    top_origin = rows[0]["origin"]
    same_tier = [r for r in rows if r["origin"] == top_origin]
    return next((r for r in same_tier if r["rubric"] != last_rubric), same_tier[0])


def due_slot(con, now_local: datetime) -> str | None:
    """Возвращает незакрытый слот, время которого уже наступило (в пределах окна), иначе None."""
    window = timedelta(minutes=config.SETTINGS["slot_window_minutes"])
    today = now_local.strftime("%Y-%m-%d")
    for hhmm in config.SETTINGS["slots"]:
        start = now_local.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
        key = f"{today} {hhmm}"
        if start <= now_local < start + window and \
                not con.execute("SELECT 1 FROM posts WHERE slot=?", (key,)).fetchone():
            return key
    return None


def _published_today(con, now_local: datetime) -> int:
    start_utc = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    return con.execute("SELECT COUNT(*) FROM posts WHERE status='published' AND published_at >= ?",
                       (config.iso(start_utc),)).fetchone()[0]


def publish_due(con, dry_run: bool = False) -> int:
    now = config.now_local()
    done = 0
    if _published_today(con, now) >= config.SETTINGS["max_posts_per_day"]:
        return 0
    last = con.execute("SELECT rubric FROM posts WHERE status='published' ORDER BY published_at DESC LIMIT 1").fetchone()
    last_rubric = last["rubric"] if last else None

    slot = due_slot(con, now)
    if slot:
        post = _next_post(con, last_rubric)
        if post and publish(con, post, slot, dry_run):
            done += 1
        return done

    # срочная новость вне расписания (не ночью и не чаще лимита)
    urgent_cfg = config.SETTINGS["urgent"]
    q_from, q_to = urgent_cfg["quiet_hours"]
    if now.hour >= q_from or now.hour < q_to:
        return 0
    start_utc = config.iso(now.replace(hour=0, minute=0, second=0, microsecond=0))
    sent = con.execute("SELECT COUNT(*) FROM posts WHERE slot='urgent' AND published_at >= ?",
                       (start_utc,)).fetchone()[0]
    post = con.execute("SELECT * FROM posts WHERE status='queued' AND origin='urgent' ORDER BY id LIMIT 1").fetchone()
    if post and sent < urgent_cfg["max_per_day"] and publish(con, post, "urgent", dry_run):
        done += 1
    return done


def expire(con) -> None:
    """Новость, не вышедшая вовремя, устаревает — старьё не публикуем."""
    ttl = config.iso(config.now_utc() - timedelta(hours=config.SETTINGS["editor"]["post_ttl_hours"]))
    con.execute("UPDATE posts SET status='expired' WHERE status='queued' AND origin IN ('auto','urgent') "
                "AND created_at < ?", (ttl,))
    con.execute("UPDATE posts SET status='expired' WHERE status='queued' AND expires_at IS NOT NULL "
                "AND expires_at < ?", (config.now_local().strftime("%Y-%m-%d"),))
    con.execute("DELETE FROM items WHERE fetched_at < ? AND status IN ('duplicate','new','skipped')",
                (config.iso(config.now_utc() - timedelta(days=14)),))
    con.commit()


# ── Резервные копии ──────────────────────────────────────────────────────────
def backup(con) -> None:
    """Ежедневная копия базы (хранятся последние 14)."""
    today = config.now_local().strftime("%Y-%m-%d")
    if db.kv_get(con, "last_backup") == today:
        return
    con.commit()
    target = config.BACKUPS / f"fursat-{today}.db"
    dst = sqlite3.connect(target)
    with dst:
        con.backup(dst)
    dst.close()
    for old in sorted(config.BACKUPS.glob("fursat-*.db"))[:-14]:
        old.unlink()
    db.kv_set(con, "last_backup", today)
    con.commit()


def restore(path: str) -> None:
    shutil.copy2(path, config.DB_PATH)
    print(f"База восстановлена из {path}")


# ── Команды администратора в Telegram ────────────────────────────────────────
def admin_commands(con) -> None:
    """Владелец пишет боту в личку: /status, /queue, /pause, /resume, /skip."""
    if not (config.BOT_TOKEN and config.ADMIN_CHAT):
        return
    try:
        offset = int(db.kv_get(con, "tg_offset", "0"))
        updates = telegram.call("getUpdates", offset=offset, timeout=0, allowed_updates=["message"])
    except telegram.TelegramError as exc:
        db.log_error(con, "admin", exc)
        return
    for upd in updates:
        db.kv_set(con, "tg_offset", str(upd["update_id"] + 1))
        msg = upd.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(config.ADMIN_CHAT):
            continue                                    # команды принимаются только от владельца
        cmd = (msg.get("text") or "").split()[0].lower() if msg.get("text") else ""
        if cmd == "/pause":
            db.kv_set(con, "paused", "1")
            telegram.notify_admin("⏸ Публикации приостановлены. /resume — продолжить.")
        elif cmd == "/resume":
            db.kv_set(con, "paused", "0")
            telegram.notify_admin("▶️ Публикации возобновлены.")
        elif cmd == "/skip":
            post = _next_post(con, None)
            if post:
                con.execute("UPDATE posts SET status='expired' WHERE id=?", (post["id"],))
                telegram.notify_admin(f"🗑 Пропущен пост #{post['id']}: {post['title']}")
        elif cmd == "/queue":
            rows = con.execute("SELECT id,origin,rubric,title FROM posts WHERE status='queued' ORDER BY "
                               "CASE origin WHEN 'urgent' THEN 0 WHEN 'auto' THEN 1 ELSE 2 END, id LIMIT 15").fetchall()
            text = "\n".join(f"#{r['id']} [{r['origin']}/{r['rubric']}] {r['title']}" for r in rows) or "Очередь пуста"
            telegram.notify_admin("📋 <b>Очередь</b>\n" + escape(text))
        elif cmd in ("/status", "/start"):
            telegram.notify_admin(dashboard.status_text(con))
    con.commit()


# ── Главный цикл ─────────────────────────────────────────────────────────────
def tick(dry_run: bool = False) -> dict:
    with db.session() as con:
        run_id = con.execute("INSERT INTO runs(started_at) VALUES(?)", (config.iso(),)).lastrowid
        con.commit()
        stats = {"collected": 0, "edited": 0, "published": 0}
        notes = []

        def stage(name, fn):
            try:
                return fn()
            except Exception as exc:               # журналируем и идём дальше: этапы независимы
                db.log_error(con, name, exc)
                notes.append(f"{name}: {type(exc).__name__}")
                return 0

        if con.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0:
            stage("seed", lambda: seed(con))
        stage("admin", lambda: admin_commands(con))

        last = db.kv_get(con, "last_collect")
        interval = timedelta(minutes=config.SETTINGS["collect"]["min_interval_minutes"])
        if not last or config.now_utc() - config.parse_iso(last) >= interval:
            stats["collected"] = stage("collect", lambda: collect.collect(con))
        stats["edited"] = stage("editor", lambda: editor.run(con))
        stage("expire", lambda: expire(con))

        if db.kv_get(con, "paused") == "1":
            notes.append("публикации на паузе")
        elif not dry_run and not (config.BOT_TOKEN and config.CHANNEL):
            notes.append("Telegram не настроен — публикация пропущена")
        else:
            stats["published"] = stage("publish", lambda: publish_due(con, dry_run))

        if not dry_run:
            stage("analytics", lambda: analytics.snapshot(con))
            stage("weekly", lambda: analytics.weekly_report(con))
        stage("backup", lambda: backup(con))

        errors_now = con.execute("SELECT COUNT(*) FROM errors WHERE ts >= (SELECT started_at FROM runs WHERE id=?)",
                                 (run_id,)).fetchone()[0]
        con.execute("UPDATE runs SET finished_at=?, ok=?, collected=?, edited=?, published=?, note=? WHERE id=?",
                    (config.iso(), int(errors_now == 0), stats["collected"], stats["edited"], stats["published"],
                     "; ".join(notes), run_id))
        con.execute("DELETE FROM runs WHERE id < ?", (run_id - 3000,))
        con.commit()
        _alert_if_stuck(con)
        stage("dashboard", lambda: dashboard.write(con))
        stats["notes"] = notes
        return stats


def _alert_if_stuck(con) -> None:
    """Сообщает владельцу, если канал молчит дольше 8 дневных часов или очередь пуста."""
    last = con.execute("SELECT MAX(published_at) AS t FROM posts WHERE status='published'").fetchone()["t"]
    queue = con.execute("SELECT COUNT(*) FROM posts WHERE status='queued'").fetchone()[0]
    now = config.now_local()
    problem = None
    if queue == 0 and not config.has_llm():
        problem = "очередь пуста, а ключ ANTHROPIC_API_KEY не задан — публиковать нечего"
    elif last and 9 <= now.hour <= 22 and config.now_utc() - config.parse_iso(last) > timedelta(hours=8):
        problem = f"нет публикаций более 8 часов (в очереди {queue})"
    if problem and db.kv_get(con, "last_alert") != now.strftime("%Y-%m-%d") + problem[:20]:
        telegram.notify_admin(f"⚠️ {config.BRAND['name']}: {problem}")
        db.kv_set(con, "last_alert", now.strftime("%Y-%m-%d") + problem[:20])
        con.commit()
