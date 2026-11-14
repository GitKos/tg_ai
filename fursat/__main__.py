"""Командный интерфейс: python -m fursat <команда>."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import analytics, collect, config, dashboard, db, images, pipeline, render, telegram

WELCOME = """✨ <b>Добро пожаловать в Fursat AI</b>

«Fursat» — это возможность. Здесь каждый день коротко и по делу:

🤖 что нового в ИИ и как этим пользоваться
💰 где и как зарабатывают в интернете
📈 идеи и новости для бизнеса в Узбекистане
⚡ технологии, которые меняют работу

<b>Наши правила</b>
— только проверенные новости со ссылкой на первоисточник
— никаких слухов и обещаний «лёгких денег»
— к каждой новости объясняем: что это значит для вас

Закрепите канал и включите уведомления, чтобы не пропустить свою возможность.

@fursat_ai"""


def cmd_tick(args):
    stats = pipeline.tick(dry_run=args.dry_run)
    print(f"собрано: {stats['collected']}, подготовлено редактором: {stats['edited']}, "
          f"опубликовано: {stats['published']}" + (f" | {'; '.join(stats['notes'])}" if stats["notes"] else ""))


def cmd_loop(args):
    """Режим для VPS: бесконечный цикл с паузой между тиками."""
    while True:
        try:
            cmd_tick(args)
        except Exception as exc:                       # цикл не должен умирать из-за одной ошибки
            print(f"[ERROR] tick: {type(exc).__name__}: {exc}")
        time.sleep(args.interval * 60)


def cmd_status(_):
    import re
    with db.session() as con:
        print(re.sub(r"</?b>", "", dashboard.status_text(con)))
        dashboard.write(con)
        print(f"HTML-панель: {config.DATA / 'status.html'}")


def cmd_report(args):
    import re
    with db.session() as con:
        print(re.sub(r"</?b>", "", analytics.report_text(con, args.days)))


def cmd_seed(_):
    with db.session() as con:
        print(f"добавлено стартовых постов: {pipeline.seed(con)}")


def cmd_preview(_):
    """Рендерит стартовые посты и обложки в data/preview — посмотреть до публикации."""
    out = config.DATA / "preview"
    out.mkdir(exist_ok=True)
    posts = json.loads((config.ROOT / "content" / "starter_posts.json").read_text(encoding="utf-8"))
    limit = config.SETTINGS["editor"]["max_caption_chars"]
    for i, p in enumerate(posts, 1):
        if p.get("kind") == "poll":
            print(f"{i:02d} ОПРОС: {p['question']}")
            continue
        fields = {k: p.get(k) for k in ("rubric", "title", "lead", "facts", "impact_heading", "impact", "tip",
                                         "source_name", "source_url", "extra_sources")}
        fields = {k: v for k, v in fields.items() if v is not None}
        text = render.fit_caption(fields, limit)
        images.make_cover(p["title"], p["rubric"], seed=p.get("source_url") or p["slug"], out=out / f"{i:02d}.jpg")
        (out / f"{i:02d}.txt").write_text(text, encoding="utf-8")
        n = len(render.visible(text))
        print(f"{i:02d} {n:>4} зн. {'OK ' if n <= 1024 else 'ДЛИННО'} [{p['rubric']}] {p['title']}")
    images.make_avatar()
    print(f"превью: {out}, аватар: {config.ASSETS / 'avatar.png'}")


def cmd_doctor(_):
    """Проверка готовности к запуску: что настроено, а что требует действий."""
    ok = True

    def check(label, good, hint=""):
        nonlocal ok
        ok &= bool(good)
        print(f"{'OK  ' if good else 'НЕТ '} {label}" + (f" — {hint}" if hint and not good else ""))

    check("TELEGRAM_BOT_TOKEN", config.BOT_TOKEN, "создайте бота у @BotFather")
    check("TELEGRAM_CHANNEL", config.CHANNEL, "например @fursat_ai")
    check("TELEGRAM_ADMIN_CHAT", config.ADMIN_CHAT, "ваш chat id — для алертов и команд (необязательно)")
    check("ANTHROPIC_API_KEY", config.has_llm(), "без него работает только очередь готовых постов")
    if config.BOT_TOKEN:
        try:
            me = telegram.call("getMe")
            print(f"OK   бот: @{me['username']}")
            if config.CHANNEL:
                member = telegram.call("getChatMember", chat_id=config.CHANNEL, user_id=me["id"])
                can_post = member.get("status") == "administrator" and member.get("can_post_messages")
                check("бот — администратор канала с правом публикации", can_post,
                      "добавьте бота в администраторы канала")
                print(f"OK   подписчиков: {telegram.subscribers()}")
        except telegram.TelegramError as exc:
            check(f"Telegram API: {exc}", False)
    with db.session() as con:
        queued = con.execute("SELECT COUNT(*) FROM posts WHERE status='queued'").fetchone()[0]
        print(f"     постов в очереди: {queued}")
    print("\nГотово к запуску." if ok else "\nЕсть незакрытые пункты (см. выше).")
    sys.exit(0 if ok else 1)


def cmd_setup(_):
    """Оформляет канал: название, описание, аватар, закреплённый приветственный пост."""
    brand = config.BRAND
    steps = [
        ("название", lambda: telegram.call("setChatTitle", chat_id=config.CHANNEL, title=brand["name"])),
        ("описание", lambda: telegram.call("setChatDescription", chat_id=config.CHANNEL,
                                           description=brand["description"].strip()[:255])),
    ]
    for label, fn in steps:
        try:
            fn()
            print(f"OK   {label}")
        except telegram.TelegramError as exc:          # «не изменено» — тоже не страшно
            print(f"ПРОПУСК {label}: {exc}")
    avatar = images.make_avatar()
    try:
        with open(avatar, "rb") as fh:
            telegram.call("setChatPhoto", files={"photo": fh}, chat_id=config.CHANNEL)
        print("OK   аватар")
    except telegram.TelegramError as exc:
        print(f"ПРОПУСК аватар: {exc}")
    with db.session() as con:
        if db.kv_get(con, "welcome_sent"):
            print("приветственный пост уже был отправлен")
            return
        cover = images.make_cover("ИИ, деньги и возможности — каждый день", "ai", seed="welcome",
                                  kicker="Fursat = возможность")
        message_id = telegram.send_post(WELCOME, cover)
        telegram.call("pinChatMessage", chat_id=config.CHANNEL, message_id=message_id, disable_notification=True)
        db.kv_set(con, "welcome_sent", str(message_id))
        print("OK   приветственный пост опубликован и закреплён")


def cmd_finance(args):
    with db.session() as con:
        con.execute("INSERT INTO finance VALUES(?,?,?,?)", (config.iso(), args.kind, args.amount, args.note))
    print(f"записано: {args.kind} ${args.amount:.2f} — {args.note}")


def cmd_backup(args):
    if args.restore:
        pipeline.restore(args.restore)
    else:
        with db.session() as con:
            db.kv_set(con, "last_backup", "")
            pipeline.backup(con)
        print(f"копия сохранена в {config.BACKUPS}")


def main():
    parser = argparse.ArgumentParser(prog="fursat", description="Fursat AI — автономная редакция Telegram-канала")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("tick", help="один проход конвейера: сбор → редактор → публикация → аналитика")
    p.add_argument("--dry-run", action="store_true", help="ничего не публиковать, только показать")
    p.set_defaults(fn=cmd_tick)
    p = sub.add_parser("loop", help="работать постоянно (для VPS)")
    p.add_argument("--interval", type=int, default=15, help="минут между проходами")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_loop)
    sub.add_parser("status", help="состояние системы").set_defaults(fn=cmd_status)
    p = sub.add_parser("report", help="аналитический отчёт")
    p.add_argument("--days", type=int, default=7)
    p.set_defaults(fn=cmd_report)
    sub.add_parser("sources", help="проверить все источники").set_defaults(fn=lambda _: collect.check_sources())
    sub.add_parser("seed", help="загрузить стартовые посты в очередь").set_defaults(fn=cmd_seed)
    sub.add_parser("preview", help="отрендерить стартовые посты и обложки").set_defaults(fn=cmd_preview)
    sub.add_parser("doctor", help="проверить готовность к запуску").set_defaults(fn=cmd_doctor)
    sub.add_parser("setup", help="оформить канал и закрепить приветствие").set_defaults(fn=cmd_setup)
    p = sub.add_parser("finance", help="записать доход или расход")
    p.add_argument("kind", choices=["income", "expense"])
    p.add_argument("amount", type=float)
    p.add_argument("note")
    p.set_defaults(fn=cmd_finance)
    p = sub.add_parser("backup", help="сделать или восстановить резервную копию")
    p.add_argument("--restore", metavar="ФАЙЛ")
    p.set_defaults(fn=cmd_backup)
    args = parser.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
