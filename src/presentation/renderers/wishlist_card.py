# -*- coding: utf-8 -*-
"""愿望单卡片：与 game_lib 同源深色列表。"""
from __future__ import annotations

import io
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw

from .game_lib import (
    BG_BOTTOM,
    BG_TOP,
    CARD_BG,
    MARGIN,
    WIDTH,
    _font,
    _truncate,
)

ROW_H = 48
MAX_ROWS = 30


def render_wishlist_card(
    player_name: str,
    items: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    note: str = "",
    ui_count: Optional[int] = None,
) -> Optional[bytes]:
    """items: [{appid, name}] 顺序即愿望单顺序。"""
    if not items:
        return None
    n = min(len(items), MAX_ROWS)
    header_h = 96
    height = header_h + n * (ROW_H + 4) + MARGIN + 36
    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 24, bold=True)
    font_name = _font(font_path, 14, bold=True)
    font_meta = _font(font_path, 12)

    title = f"{player_name} 的愿望单"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    extra = f"公开 SSR {len(items)} 条"
    if ui_count:
        extra += f" · 客户端约 {ui_count}"
    sub = f"{datetime.now().strftime('%Y/%m/%d')} · {extra}"
    if note:
        sub += f" · {note}"
    bbox2 = draw.textbbox((0, 0), sub, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 48), sub, font=font_meta, fill=(143, 152, 160, 255))

    y = header_h
    for idx, item in enumerate(items[:MAX_ROWS]):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=8, fill=CARD_BG + (255,))
        bar = (102, 192, 244, 255) if idx < 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 8, x0 + 10, y + ROW_H - 8), radius=2, fill=bar)
        num = f"{idx + 1:02d}"
        draw.text((x0 + 18, y + 14), num, font=font_meta, fill=(140, 150, 160, 255))
        name = _truncate(draw, str(item.get("name") or item.get("appid") or "?"), font_name, WIDTH - 90)
        draw.text((x0 + 52, y + 14), name, font=font_name, fill=(255, 255, 255, 255))
        y += ROW_H + 4

    if len(items) > MAX_ROWS:
        draw.text(
            (MARGIN, height - 28),
            f"仅展示前 {MAX_ROWS} 条（SSR 共 {len(items)}）",
            font=font_meta,
            fill=(140, 150, 160, 255),
        )

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
