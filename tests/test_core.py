import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ["FURSAT_DB"] = str(Path(tempfile.mkdtemp()) / "test.db")

from fursat import analytics, collect, config, db, images, pipeline, render  # noqa: E402

SRC_WORLD = {"name": "Test", "trust": 2, "region": "world", "topic": "tech"}
SRC_UZ = {"name": "TestUz", "trust": 3, "region": "uz", "topic": "uzbekistan"}


def test_normalize_url_strips_tracking():
    assert collect.normalize_url("https://www.Site.com/a/?utm_source=x&id=5#top") == "https://site.com/a?id=5"


def test_same_story_different_wording_is_duplicate():
    a = collect.title_key("ChatGPT Wallet lets OpenAI's Dots agents make purchases for you")
    b = collect.title_key("ChatGPT's new Wallet lets AI agents shop and check out for you")
    c = collect.title_key("SpaceX launches Starship in key test flight of massive rocket")
    assert collect.similar(a, b) >= 0.62
    assert collect.similar(a, c) < 0.62


def test_prescore_relevance_and_rumor():
    now = config.now_utc()
    ai, topic, rumor = collect.prescore("OpenAI launches new ChatGPT agent for small business", "", SRC_WORLD, now)
    crime, _, _ = collect.prescore("В Ташкенте задержали мужчину", "", SRC_UZ, now)
    _, _, is_rumor = collect.prescore("Apple reportedly plans a new AI device", "", SRC_WORLD, now)
    assert topic == "ai" and ai > 45 > crime
    assert not rumor and is_rumor


def test_local_generic_news_is_not_relevant():
    score, _, _ = collect.prescore("Узбекистан и Таджикистан обсудили товарооборот", "", SRC_UZ, config.now_utc())
    assert score < config.SETTINGS["editor"]["min_prescore"]


def test_render_escapes_and_fits_caption():
    fields = dict(rubric="ai", title="A <b> & B", lead="Лид " * 60, facts=["факт " * 20] * 4,
                  impact_heading="Почему", impact="Польза " * 30, tip="Совет " * 30,
                  source_name="Src", source_url="https://example.com/?a=1&b=2")
    text = render.fit_caption(fields, 1000)
    assert "&lt;b&gt;" in text and "&amp;b=2" in text
    assert "💡" not in text                      # совет убран, чтобы уложиться в лимит
    assert render.has_banned("гарантированный доход") and not render.has_banned("обычный текст")


def test_starter_posts_fit_telegram_caption_and_seed_once():
    with db.session() as con:
        first = pipeline.seed(con)
        assert first >= 1 and pipeline.seed(con) == 0          # повторная загрузка не создаёт дублей
        for row in con.execute("SELECT body, kind FROM posts"):
            if row["kind"] == "post":
                assert len(render.visible(row["body"])) <= 1024


def test_slot_is_filled_only_once():
    with db.session() as con:
        pipeline.seed(con)
        now = datetime(2026, 11, 16, 8, 10, tzinfo=config.TZ)
        slot = pipeline.due_slot(con, now)
        assert slot == "2026-11-16 08:00"
        con.execute("UPDATE posts SET slot=?, status='published' WHERE id=(SELECT MIN(id) FROM posts)", (slot,))
        assert pipeline.due_slot(con, now) is None
        assert pipeline.due_slot(con, now + timedelta(hours=3)) == "2026-11-16 10:30"
        assert pipeline.due_slot(con, now.replace(hour=3)) is None   # ночью слотов нет


def test_cover_is_deterministic_and_unique(tmp_path):
    a = images.make_cover("Заголовок новости", "ai", seed="url-1", out=tmp_path / "a.jpg")
    b = images.make_cover("Заголовок новости", "ai", seed="url-1", out=tmp_path / "b.jpg")
    c = images.make_cover("Заголовок новости", "ai", seed="url-2", out=tmp_path / "c.jpg")
    assert a.read_bytes() == b.read_bytes() != c.read_bytes()


def test_view_counter_parsing():
    assert analytics._number("1.2K") == 1200 and analytics._number("3M") == 3_000_000 and analytics._number("87") == 87
