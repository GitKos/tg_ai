"""SQLite-хранилище: новости, публикации, метрики, журнал запусков и ошибок."""
from __future__ import annotations

import sqlite3
import traceback
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id            INTEGER PRIMARY KEY,
    url           TEXT NOT NULL UNIQUE,      -- оригинальная ссылка (нормализованная)
    source        TEXT NOT NULL,
    source_trust  INTEGER NOT NULL,
    region        TEXT NOT NULL,
    title         TEXT NOT NULL,
    title_key     TEXT NOT NULL,             -- нормализованные слова заголовка для поиска дублей
    summary       TEXT,                      -- краткое содержание из ленты
    published_at  TEXT,                      -- дата публикации в источнике
    event_date    TEXT,                      -- дата события, если отличается (заполняет редактор)
    fetched_at    TEXT NOT NULL,
    topic         TEXT,
    score         REAL DEFAULT 0,            -- оценка интереса/вирусности 0..100
    confirmations INTEGER DEFAULT 0,         -- сколько других источников пишут о том же
    credibility   INTEGER,                   -- оценка достоверности 0..100
    verify_status TEXT DEFAULT 'unverified', -- unverified | verified | single_source | rumor | rejected
    verify_note   TEXT,
    status        TEXT DEFAULT 'new',        -- new | duplicate | skipped | rejected | used
    dup_of        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status, score);

CREATE TABLE IF NOT EXISTS posts (
    id            INTEGER PRIMARY KEY,
    slug          TEXT UNIQUE,               -- для стартовых постов (защита от повторной загрузки)
    item_id       INTEGER REFERENCES items(id),
    origin        TEXT NOT NULL,             -- starter | auto | urgent | digest
    rubric        TEXT NOT NULL,
    title         TEXT NOT NULL,
    body          TEXT NOT NULL,             -- готовый HTML для Telegram
    kind          TEXT DEFAULT 'post',       -- post | poll
    poll_options  TEXT,                      -- JSON-массив для опросов
    source_name   TEXT,
    source_url    TEXT,
    image_path    TEXT,
    status        TEXT DEFAULT 'queued',     -- queued | published | failed | expired
    slot          TEXT,                      -- 'YYYY-MM-DD HH:MM' по Ташкенту или 'urgent'
    created_at    TEXT NOT NULL,
    expires_at    TEXT,                      -- YYYY-MM-DD: после этой даты пост не публикуется
    published_at  TEXT,
    tg_message_id INTEGER,
    views         INTEGER,
    reactions     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_posts_status ON posts(status);

CREATE TABLE IF NOT EXISTS metrics (        -- история просмотров поста
    post_id INTEGER, ts TEXT, views INTEGER, reactions INTEGER
);
CREATE TABLE IF NOT EXISTS channel_stats (ts TEXT, subscribers INTEGER);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, ok INTEGER,
    collected INTEGER DEFAULT 0, edited INTEGER DEFAULT 0, published INTEGER DEFAULT 0, note TEXT
);
CREATE TABLE IF NOT EXISTS errors (ts TEXT, stage TEXT, message TEXT);
CREATE TABLE IF NOT EXISTS source_health (
    source TEXT PRIMARY KEY, last_ok TEXT, last_error TEXT, last_count INTEGER, fails INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS llm_usage (ts TEXT, model TEXT, input_tokens INTEGER, output_tokens INTEGER);
CREATE TABLE IF NOT EXISTS finance (ts TEXT, kind TEXT, amount_usd REAL, note TEXT); -- income | expense
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def connect() -> sqlite3.Connection:
    con = sqlite3.connect(config.DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


@contextmanager
def session():
    con = connect()
    try:
        yield con
        con.commit()
    finally:
        con.close()


def kv_get(con, key: str, default: str | None = None) -> str | None:
    row = con.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def kv_set(con, key: str, value: str) -> None:
    con.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value))


def log_error(con, stage: str, exc: BaseException | str) -> None:
    """Журнал ошибок: сбой одного этапа не должен останавливать весь конвейер."""
    if isinstance(exc, BaseException):
        message = f"{type(exc).__name__}: {exc}\n{''.join(traceback.format_tb(exc.__traceback__)[-2:])}"
    else:
        message = exc
    con.execute("INSERT INTO errors VALUES(?,?,?)", (config.iso(), stage, message[:2000]))
    con.commit()
    print(f"[ERROR] {stage}: {message.splitlines()[0]}")
