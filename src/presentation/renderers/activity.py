# -*- coding: utf-8 -*-
"""购游戏日志卡片渲染 — 深色风格，与 rank/list 同源，支持封面"""
import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw, ImageFont

from ...shared.fonts import load_truetype

BG_TOP = (44, 62, 80)
BG_BOTTOM = (24, 32, 44)
CARD_BG = (38, 44, 56)
WIDTH = 680
MARGIN = 18
ROW_H = 80
COVER_W = 120
COVER_H = 60


def _font(path, size, bold=False):
    name = "NotoSansHans-Medium.otf" if bold else "NotoSansHans-Regular.otf"
    extra = ()
    if path:
        extra = (str(path).replace("Regular", "Medium") if bold else path,)
    return load_truetype(name, size, fallbacks=extra)


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _truncate(draw, text, font, max_w):
    text = str(text or "")
    bbox = draw.textbbox((0, 0), text, font=font)
    if bbox[2] - bbox[0] <= max_w:
        return text
    while text:
        text = text[:-1]
        bbox = draw.textbbox((0, 0), text + "…", font=font)
        if bbox[2] - bbox[0] <= max_w:
            return text + "…"
    return "…"


async def render_activity_image(
    title: str,
    items: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    covers: Optional[Dict[str, str]] = None,
    proxy: Optional[str] = None,
) -> Optional[bytes]:
    """渲染购游戏日志卡片，返回 PNG bytes。

    items: [{"name": str, "game_name": str, "appid": str, "date": str}]
    covers: {appid: 本地封面路径}
    """
    if not items:
        return None
    n = len(items)
    header_h = 80
    height = header_h + n * (ROW_H + 8) + MARGIN + 30

    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 24, bold=True)
    font_name = _font(font_path, 17, bold=True)
    font_game = _font(font_path, 14)
    font_meta = _font(font_path, 12)

    # 标题
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 14), title, font=font_title, fill=(255, 255, 255, 255))
    now_str = datetime.now().strftime("%Y/%m/%d")
    subtitle = f"{now_str} · 共 {n} 条"
    bbox2 = draw.textbbox((0, 0), subtitle, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 48), subtitle, font=font_meta, fill=(143, 152, 160, 255))

    y = header_h
    for item in items:
        x0 = MARGIN
        x1 = WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        # 左侧绿色条
        draw.rounded_rectangle((x0 + 6, y + 12, x0 + 10, y + ROW_H - 12), radius=3, fill=(120, 220, 100, 255))

        # 封面（右侧）
        cover_path = covers.get(str(item.get("appid") or "")) if covers else None
        cover_x = x1 - COVER_W - 14
        cover_y = y + (ROW_H - COVER_H) // 2
        has_cover = False
        if cover_path and os.path.isfile(cover_path):
            try:
                cimg = Image.open(cover_path).convert("RGB")
                cimg = _fit_cover(cimg, COVER_W, COVER_H)
                draw.rounded_rectangle(
                    (cover_x - 1, cover_y - 1, cover_x + COVER_W + 1, cover_y + COVER_H + 1),
                    radius=6, outline=(255, 255, 255, 160), width=1,
                )
                img.paste(cimg, (cover_x, cover_y))
                has_cover = True
            except Exception:
                pass

        # 文字区域左边界
        text_x = x0 + 22
        # 右侧文字截止：有封面时到封面左边，否则到卡片右边
        text_right = (cover_x - 14) if has_cover else (x1 - 14)

        # 第一行：玩家名（左） + 日期（右对齐）
        name = _truncate(draw, item.get("name", "?"), font_name, 160)
        draw.text((text_x, y + 16), name, font=font_name, fill=(255, 255, 255, 255))
        date_str = str(item.get("date", ""))
        date_bbox = draw.textbbox((0, 0), date_str, font=font_meta)
        date_w = date_bbox[2] - date_bbox[0]
        draw.text((text_right - date_w, y + 20), date_str, font=font_meta, fill=(140, 150, 160, 255))

        # 第二行：游戏名
        game = _truncate(draw, item.get("game_name", "?"), font_game, text_right - text_x)
        draw.text((text_x, y + 46), f"《{game}》", font=font_game, fill=(180, 220, 255, 255))

        y += ROW_H + 8

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
