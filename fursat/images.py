"""Генерация фирменных изображений: обложки постов и аватар канала.

Картинки рисуются программно (Pillow): единый стиль, уникальный узор для каждой новости,
никаких чужих фотографий, логотипов и недостоверных изображений.
"""
from __future__ import annotations

import hashlib
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import config

W, H = 1280, 720
FONT = config.ASSETS / "fonts" / "Manrope.ttf"
BG = (11, 13, 28)


def font(size: int, weight: int = 800) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(str(FONT), size)
    f.set_variation_by_axes([weight])
    return f


def _rgb(hex_color: str) -> tuple[int, int, int]:
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t: float):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _background(size, c1, c2, rnd: random.Random) -> Image.Image:
    """Тёмный фон с цветными световыми пятнами и геометрическим узором — у каждой новости свой."""
    w, h = size
    img = Image.new("RGB", size, BG)
    glow = Image.new("RGB", size, BG)
    g = ImageDraw.Draw(glow)
    for color, _ in ((c1, 0), (c2, 1), (_mix(c1, c2, 0.5), 2)):
        x, y, r = rnd.randint(int(w * 0.35), w), rnd.randint(-h // 4, h), rnd.randint(h // 3, int(h * 0.75))
        g.ellipse((x - r, y - r, x + r, y + r), fill=_mix(BG, color, 0.75))
    img = Image.blend(img, glow.filter(ImageFilter.GaussianBlur(h // 5)), 0.9)

    layer = Image.new("RGBA", size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    pattern = rnd.choice(("rings", "grid", "bars", "nodes"))
    if pattern == "rings":
        cx, cy = rnd.randint(int(w * 0.65), int(w * 0.95)), rnd.randint(int(h * 0.2), int(h * 0.8))
        for i in range(1, 9):
            r = i * h // 9
            d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=(255, 255, 255, 26), width=2)
    elif pattern == "grid":
        step = rnd.choice((48, 64, 80))
        for x in range(w // 2, w, step):
            for y in range(0, h, step):
                if rnd.random() < 0.55:
                    s = rnd.choice((3, 4, 6))
                    d.ellipse((x - s, y - s, x + s, y + s), fill=(255, 255, 255, rnd.randint(30, 90)))
    elif pattern == "bars":
        x = int(w * 0.62)
        while x < w:
            bw, bh = rnd.randint(18, 46), rnd.randint(h // 6, int(h * 0.8))
            d.rounded_rectangle((x, h - bh, x + bw, h + 20), radius=10, fill=(255, 255, 255, rnd.randint(18, 48)))
            x += bw + rnd.randint(14, 30)
    else:
        pts = [(rnd.randint(int(w * 0.55), w - 30), rnd.randint(30, h - 30)) for _ in range(11)]
        for i, p in enumerate(pts):
            for q in pts[i + 1:i + 3]:
                d.line((p, q), fill=(255, 255, 255, 34), width=2)
        for p in pts:
            s = rnd.randint(5, 11)
            d.ellipse((p[0] - s, p[1] - s, p[0] + s, p[1] + s), fill=(255, 255, 255, 110))
    img = Image.alpha_composite(img.convert("RGBA"), layer)

    # затемнение слева — под текст, чтобы заголовок читался на телефоне
    shade = Image.new("L", size, 0)
    sd = ImageDraw.Draw(shade)
    for x in range(w):
        sd.line((x, 0, x, h), fill=int(205 * max(0.0, 1 - x / (w * 0.85))))
    dark = Image.new("RGBA", size, BG + (255,))
    return Image.composite(dark, img, shade).convert("RGB")


def _wrap(draw, text: str, fnt, max_width: int) -> list[str]:
    lines, line = [], ""
    for word in text.split():
        trial = f"{line} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            line = trial
        else:
            if line:
                lines.append(line)
            line = word
    return lines + [line] if line else lines


def _spark(d: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    """Четырёхлучевая искра — фирменный знак (искра идеи / ИИ)."""
    k = r * 0.24
    d.polygon([(cx, cy - r), (cx + k, cy - k), (cx + r, cy), (cx + k, cy + k),
               (cx, cy + r), (cx - k, cy + k), (cx - r, cy), (cx - k, cy - k)], fill=fill)


def make_cover(title: str, rubric: str, seed: str, out: Path | None = None, kicker: str | None = None) -> Path:
    """Обложка поста 1280×720. seed делает узор уникальным и воспроизводимым для конкретной новости."""
    meta = config.RUBRICS.get(rubric, config.RUBRICS["tech"])
    c1, c2 = _rgb(meta["color"][0]), _rgb(meta["color"][1])
    rnd = random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16))
    img = _background((W, H), c1, c2, rnd)
    d = ImageDraw.Draw(img)
    pad = 72

    # плашка рубрики
    label = (kicker or meta["label"]).upper()
    lf = font(30, 800)
    lw = d.textlength(label, font=lf)
    d.rounded_rectangle((pad, 64, pad + lw + 52, 64 + 58), radius=29, fill=_mix(c1, c2, 0.35))
    d.text((pad + 26, 64 + 29), label, font=lf, fill="white", anchor="lm")

    # заголовок: подбираем кегль так, чтобы уместиться в 5 строк
    max_w = int(W * 0.74)
    for size in (84, 74, 66, 58, 52, 46, 40):
        tf = font(size, 800)
        lines = _wrap(d, title, tf, max_w)
        if len(lines) * size * 1.14 <= H - 64 - 58 - 60 - 150 and len(lines) <= 5:
            break
    y = 64 + 58 + 54
    for line in lines[:6]:
        d.text((pad, y), line, font=tf, fill="white")
        y += int(size * 1.14)

    # подвал с брендом
    d.rectangle((pad, H - 118, pad + 96, H - 112), fill=_mix(c1, c2, 0.5))
    _spark(d, pad + 20, H - 62, 22, _mix(c2, (255, 255, 255), 0.25))
    d.text((pad + 54, H - 62), config.BRAND["name"], font=font(34, 800), fill="white", anchor="lm")
    handle = "@" + config.BRAND["username"]
    d.text((W - pad, H - 62), handle, font=font(28, 600), fill=(255, 255, 255, 200), anchor="rm")

    out = out or config.IMAGES / f"{hashlib.md5(seed.encode()).hexdigest()[:16]}.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=90)
    return out


def make_avatar(out: Path | None = None) -> Path:
    """Аватар канала 640×640: искра на градиенте + буква F."""
    size = 640
    rnd = random.Random(7)
    img = _background((size, size), _rgb("#5B2EFF"), _rgb("#00C2FF"), rnd)
    base = Image.new("RGB", (size, size))
    bd = ImageDraw.Draw(base)
    a, b = _rgb("#3A1FD1"), _rgb("#00B8E6")
    for y in range(size):
        bd.line((0, y, size, y), fill=_mix(a, b, y / size))
    img = Image.blend(base, img, 0.35)
    d = ImageDraw.Draw(img)
    d.text((size // 2 - 36, size // 2 + 6), "F", font=font(400, 800), fill="white", anchor="mm")
    _spark(d, size // 2 + 138, size // 2 - 128, 74, (182, 245, 0))
    _spark(d, size // 2 + 206, size // 2 - 22, 28, (255, 255, 255))
    out = out or config.ASSETS / "avatar.png"
    img.save(out, "PNG")
    return out
