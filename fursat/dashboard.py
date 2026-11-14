"""Панель контроля: текстовый статус (консоль, Telegram /status, сводка GitHub Actions) и HTML-страница."""
from __future__ import annotations

import os
from datetime import timedelta
from html import escape

from . import config, db


def collect_state(con) -> dict:
    day = config.iso(config.now_utc() - timedelta(hours=24))
    one = lambda q, *a: con.execute(q, a).fetchone()[0]
    last_run = con.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    subs = con.execute("SELECT subscribers FROM channel_stats ORDER BY ts DESC LIMIT 1").fetchone()
    return {
        "last_run": dict(last_run) if last_run else None,
        "paused": db.kv_get(con, "paused") == "1",
        "telegram": bool(config.BOT_TOKEN and config.CHANNEL),
        "llm": config.has_llm(),
        "subscribers": subs["subscribers"] if subs else None,
        "queue": one("SELECT COUNT(*) FROM posts WHERE status='queued'"),
        "queue_auto": one("SELECT COUNT(*) FROM posts WHERE status='queued' AND origin!='starter'"),
        "published_24h": one("SELECT COUNT(*) FROM posts WHERE status='published' AND published_at>=?", day),
        "published_total": one("SELECT COUNT(*) FROM posts WHERE status='published'"),
        "failed": one("SELECT COUNT(*) FROM posts WHERE status='failed'"),
        "items_24h": one("SELECT COUNT(*) FROM items WHERE fetched_at>=?", day),
        "dups_24h": one("SELECT COUNT(*) FROM items WHERE fetched_at>=? AND status='duplicate'", day),
        "rejected_24h": one("SELECT COUNT(*) FROM items WHERE fetched_at>=? AND status='rejected'", day),
        "errors_24h": one("SELECT COUNT(*) FROM errors WHERE ts>=?", day),
        "llm_calls_24h": one("SELECT COUNT(*) FROM llm_usage WHERE ts>=?", day),
        "errors": [dict(r) for r in con.execute("SELECT * FROM errors ORDER BY ts DESC LIMIT 8")],
        "sources": [dict(r) for r in con.execute("SELECT * FROM source_health ORDER BY fails DESC, source")],
        "recent": [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE status='published' ORDER BY published_at DESC LIMIT 15")],
        "next": [dict(r) for r in con.execute(
            "SELECT * FROM posts WHERE status='queued' ORDER BY CASE origin WHEN 'urgent' THEN 0 "
            "WHEN 'auto' THEN 1 ELSE 2 END, id LIMIT 8")],
    }


def _local(ts: str | None) -> str:
    return config.parse_iso(ts).astimezone(config.TZ).strftime("%d.%m %H:%M") if ts else "—"


def health(s: dict) -> tuple[str, str]:
    """Общий индикатор: 🟢 работает / 🟡 есть замечания / 🔴 требует вмешательства."""
    if not s["telegram"]:
        return "🔴", "Telegram не подключён: задайте TELEGRAM_BOT_TOKEN и TELEGRAM_CHANNEL"
    if s["queue"] == 0 and not s["llm"]:
        return "🔴", "Очередь пуста и нет ключа ANTHROPIC_API_KEY"
    if s["paused"]:
        return "🟡", "Публикации на паузе (/resume)"
    if not s["llm"]:
        return "🟡", f"AI-редактор выключен (нет ANTHROPIC_API_KEY); в очереди {s['queue']} готовых постов"
    dead = [x["source"] for x in s["sources"] if x["fails"] >= 6]
    if dead:
        return "🟡", "Источники не отвечают: " + ", ".join(dead[:4])
    if s["errors_24h"] > 10 or s["failed"]:
        return "🟡", f"Ошибок за сутки: {s['errors_24h']}, неотправленных постов: {s['failed']}"
    return "🟢", "Система работает штатно"


def status_text(con) -> str:
    s = collect_state(con)
    icon, message = health(s)
    run = s["last_run"]
    return "\n".join([
        f"{icon} <b>{config.BRAND['name']}</b> — {escape(message)}",
        f"Последний запуск: {_local(run['finished_at']) if run else '—'}"
        + (f" ({escape(run['note'])})" if run and run["note"] else ""),
        f"Подписчики: {s['subscribers'] if s['subscribers'] is not None else '—'}",
        f"Опубликовано: {s['published_24h']} за сутки, {s['published_total']} всего",
        f"Очередь: {s['queue']} (из них новостей редактора: {s['queue_auto']})",
        f"Собрано за сутки: {s['items_24h']}, дублей отсеяно: {s['dups_24h']}, отклонено: {s['rejected_24h']}",
        f"Вызовов LLM за сутки: {s['llm_calls_24h']}, ошибок: {s['errors_24h']}",
    ])


def write(con) -> None:
    s = collect_state(con)
    icon, message = health(s)
    rub = lambda r: config.RUBRICS.get(r, {}).get("label", r)
    rows = lambda items, fn: "".join(fn(x) for x in items) or '<tr><td colspan="5">—</td></tr>'
    cards = [("Подписчики", s["subscribers"] if s["subscribers"] is not None else "—"),
             ("Постов за 24 ч", s["published_24h"]), ("В очереди", s["queue"]),
             ("Собрано за 24 ч", s["items_24h"]), ("Дублей отсеяно", s["dups_24h"]),
             ("Ошибок за 24 ч", s["errors_24h"])]
    page = f"""<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{config.BRAND['name']} — панель</title>
<style>body{{font:15px/1.5 system-ui,sans-serif;margin:0;padding:24px;background:#0b0d1c;color:#e8eaf6}}
h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:16px;margin:28px 0 8px;color:#9fa8da}}
.st{{padding:12px 16px;border-radius:12px;background:#171a33;margin:12px 0}}
.g{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px}}
.c{{background:#171a33;border-radius:12px;padding:14px}}.c b{{display:block;font-size:26px}}
.c span{{color:#9fa8da;font-size:13px}}table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{text-align:left;padding:6px 8px;border-bottom:1px solid #262a4d;vertical-align:top}}
th{{color:#9fa8da;font-weight:500}}.w{{overflow-x:auto}}small{{color:#7986cb}}</style>
<h1>{config.BRAND['name']} — панель состояния</h1>
<small>Обновлено {config.now_local():%d.%m.%Y %H:%M} (Ташкент)</small>
<div class="st">{icon} {escape(message)}</div>
<div class="g">{''.join(f'<div class="c"><b>{v}</b><span>{k}</span></div>' for k, v in cards)}</div>
<h2>Следующие в очереди</h2><div class="w"><table><tr><th>#</th><th>Тип</th><th>Рубрика</th><th>Заголовок</th></tr>
{rows(s['next'], lambda p: f"<tr><td>{p['id']}</td><td>{p['origin']}</td><td>{rub(p['rubric'])}</td><td>{escape(p['title'])}</td></tr>")}
</table></div>
<h2>Последние публикации</h2><div class="w"><table><tr><th>Время</th><th>Рубрика</th><th>Заголовок</th><th>Просмотры</th><th>Реакции</th></tr>
{rows(s['recent'], lambda p: f"<tr><td>{_local(p['published_at'])}</td><td>{rub(p['rubric'])}</td><td>{escape(p['title'])}</td><td>{p['views'] or '—'}</td><td>{p['reactions'] or '—'}</td></tr>")}
</table></div>
<h2>Источники</h2><div class="w"><table><tr><th>Источник</th><th>Последний успех</th><th>Записей</th><th>Сбоев подряд</th><th>Ошибка</th></tr>
{rows(s['sources'], lambda x: f"<tr><td>{escape(x['source'])}</td><td>{_local(x['last_ok'])}</td><td>{x['last_count'] or '—'}</td><td>{x['fails']}</td><td>{escape((x['last_error'] or '')[:90]) if x['fails'] else ''}</td></tr>")}
</table></div>
<h2>Журнал ошибок</h2><div class="w"><table><tr><th>Время</th><th>Этап</th><th>Сообщение</th></tr>
{rows(s['errors'], lambda e: f"<tr><td>{_local(e['ts'])}</td><td>{escape(e['stage'])}</td><td>{escape(e['message'].splitlines()[0][:160])}</td></tr>")}
</table></div></html>"""
    (config.DATA / "status.html").write_text(page, encoding="utf-8")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")       # сводка прямо на странице запуска Actions
    if summary_path:
        import re
        plain = re.sub(r"</?b>", "**", status_text(con)).replace("\n", "  \n")
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(plain + "\n")
