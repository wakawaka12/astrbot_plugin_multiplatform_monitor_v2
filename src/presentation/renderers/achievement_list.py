# -*- coding: utf-8 -*-
"""全成就列表卡片：解锁/未解锁一目了然，深色风格与成就推送卡同源。"""
from __future__ import annotations

import asyncio
import io
import os
from typing import Any, Dict, Iterable, List, Optional, Set

import aiohttp
from PIL import Image, ImageDraw

from ...shared.fonts import load_truetype
from ...shared.logging import logger
from ...shared.network import aiohttp_connector
from ...shared.paths import IMAGES_DIR

ROW_H = 72
ICON = 48
WIDTH = 420
PADDING = 16
MAX_ROWS = 40


def _wrap(draw, text, font, max_w):
    lines = []
    line = ""
    for ch in str(text or ""):
        if draw.textlength(line + ch, font=font) <= max_w:
            line += ch
        else:
            if line:
                lines.append(line)
            line = ch
    if line:
        lines.append(line)
    return lines or [""]


async def _load_icon(session, urls: Iterable[str], proxy=None, size=ICON) -> Optional[Image.Image]:
    for url in urls:
        if not url:
            continue
        try:
            async with session.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    img = Image.open(io.BytesIO(data)).convert("RGBA")
                    img = img.resize((size, size), Image.LANCZOS)
                    mask = Image.new("L", (size, size), 0)
                    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius=8, fill=255)
                    img.putalpha(mask)
                    return img
        except Exception:
            continue
    return None


async def render_achievement_list_image(
    achievement_details: Dict[str, Any],
    unlocked_set: Set[str],
    player_name: str = "",
    game_name: str = "",
    font_path: Optional[str] = None,
    proxy: Optional[str] = None,
    limit: int = MAX_ROWS,
) -> Optional[bytes]:
    """渲染某玩家在某游戏的全成就列表。

    achievement_details: apiname -> {name, description, icon, icon_gray, percent}
    unlocked_set: 已解锁 apiname 集合
    """
    if not achievement_details:
        return None

    unlocked_set = set(unlocked_set or set())
    items = list(achievement_details.items())
    # 已解锁优先，其次按全球解锁率升序（稀有在前）
    def sort_key(pair):
        apiname, detail = pair
        unlocked = 0 if apiname in unlocked_set else 1
        try:
            pct = float(detail.get("percent")) if detail.get("percent") is not None else 999.0
        except (TypeError, ValueError):
            pct = 999.0
        return (unlocked, pct, str(detail.get("name") or apiname))

    items.sort(key=sort_key)
    total = len(items)
    unlocked_n = sum(1 for a, _ in items if a in unlocked_set)
    shown = items[:limit]

    extra = (font_path,) if font_path else ()
    font_title = load_truetype("NotoSansHans-Medium.otf", 20, fallbacks=extra)
    font_game = load_truetype("NotoSansHans-Regular.otf", 13, fallbacks=extra)
    font_name = load_truetype("NotoSansHans-Medium.otf", 14, fallbacks=extra)
    font_desc = load_truetype("NotoSansHans-Regular.otf", 12, fallbacks=extra)
    font_meta = load_truetype("NotoSansHans-Regular.otf", 11, fallbacks=extra)

    dummy = ImageDraw.Draw(Image.new("RGB", (8, 8)))
    title = f"{player_name} 的成就进度"
    header_h = 78
    rows_h = ROW_H * len(shown)
    footer_h = 28 if total > limit else 0
    height = PADDING + header_h + 8 + rows_h + 10 + footer_h + PADDING

    img = Image.new("RGBA", (WIDTH, height), (20, 26, 33, 255))
    draw = ImageDraw.Draw(img)

    draw.text((PADDING, PADDING), title, fill=(255, 255, 255), font=font_title)
    draw.text((PADDING, PADDING + 28), game_name or "未知游戏", fill=(160, 160, 160), font=font_game)
    progress = int(unlocked_n / total * 100) if total else 0
    bar_y = PADDING + 50
    bar_w = WIDTH - PADDING * 2
    draw.rounded_rectangle((PADDING, bar_y, PADDING + bar_w, bar_y + 10), radius=5, fill=(60, 62, 70, 200))
    fill_w = int(bar_w * progress / 100)
    if fill_w > 0:
        draw.rounded_rectangle((PADDING, bar_y, PADDING + fill_w, bar_y + 10), radius=5, fill=(26, 159, 255, 255))
    ptxt = f"{unlocked_n}/{total}（{progress}%）"
    pw = dummy.textlength(ptxt, font=font_meta)
    draw.text((WIDTH - PADDING - pw, bar_y - 2), ptxt, fill=(142, 207, 255), font=font_meta)

    connector = aiohttp_connector()
    y = PADDING + header_h + 8
    async with aiohttp.ClientSession(connector=connector) as session:
        for apiname, detail in shown:
            unlocked = apiname in unlocked_set
            name = detail.get("name") or apiname
            desc = detail.get("description") or ""
            percent = detail.get("percent")
            try:
                pct_val = float(percent) if percent is not None else None
            except (TypeError, ValueError):
                pct_val = None
            pct_str = f"全球 {pct_val:.1f}%" if pct_val is not None else ""

            card_x0, card_x1 = PADDING, WIDTH - PADDING
            card = Image.new("RGBA", (card_x1 - card_x0, ROW_H - 6), (35, 38, 46, 230))
            mask = Image.new("L", card.size, 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, card.size[0] - 1, card.size[1] - 1), radius=8, fill=255)
            if unlocked:
                # 左侧蓝条
                ImageDraw.Draw(card).rounded_rectangle((0, 8, 3, card.size[1] - 8), radius=2, fill=(26, 159, 255, 255))
            else:
                ImageDraw.Draw(card).rounded_rectangle((0, 8, 3, card.size[1] - 8), radius=2, fill=(70, 75, 88, 255))
            img.paste(card, (card_x0, y), mask)

            icon_urls = []
            if unlocked:
                icon_urls = [detail.get("icon"), detail.get("icon_gray")]
            else:
                icon_urls = [detail.get("icon_gray"), detail.get("icon")]
            # 域名兜底
            more = []
            for u in icon_urls:
                if u:
                    more.append(u.replace("media.steamstatic.com", "cdn.akamai.steamstatic.com"))
                    more.append(u.replace("steamcdn-a.akamaihd.net", "cdn.akamai.steamstatic.com"))
            icon_urls = [u for u in list(dict.fromkeys([*icon_urls, *more])) if u]
            icon_img = await _load_icon(session, icon_urls, proxy=proxy)
            if icon_img is None and not unlocked:
                # 未解锁且无灰图：用彩色图再压暗
                color = await _load_icon(session, [detail.get("icon")], proxy=proxy)
                if color is not None:
                    gray = Image.new("RGBA", color.size, (90, 90, 90, 255))
                    gray.putalpha(color.getchannel("A"))
                    icon_img = Image.blend(color, gray, 0.55)
            if icon_img is None:
                try:
                    ph = str(IMAGES_DIR / "unknown_avatar.jpg")
                    if os.path.exists(ph):
                        icon_img = Image.open(ph).convert("RGBA").resize((ICON, ICON), Image.LANCZOS)
                except Exception:
                    pass

            icon_x = card_x0 + 10
            icon_y = y + (ROW_H - 6 - ICON) // 2
            if icon_img:
                img.alpha_composite(icon_img, (icon_x, icon_y))
            else:
                draw.rounded_rectangle((icon_x, icon_y, icon_x + ICON, icon_y + ICON), radius=8, fill=(60, 70, 85, 255))

            text_x = icon_x + ICON + 12
            text_w = WIDTH - PADDING - text_x - 8
            name_color = (255, 255, 255, 255) if unlocked else (170, 170, 170, 255)
            desc_color = (187, 187, 187, 255) if unlocked else (120, 120, 120, 255)
            name_lines = _wrap(dummy, name, font_name, text_w)[:1]
            desc_lines = _wrap(dummy, desc, font_desc, text_w)[:2]
            ty = y + 8
            for i, line in enumerate(name_lines):
                draw.text((text_x, ty + i * 18), line, fill=name_color, font=font_name)
            dy = ty + 18
            for i, line in enumerate(desc_lines):
                draw.text((text_x, dy + i * 16), line, fill=desc_color, font=font_desc)
            if pct_str:
                draw.text((text_x, dy + len(desc_lines) * 16), pct_str, fill=(142, 207, 255, 220), font=font_meta)
            # 右侧状态点
            status = "✓" if unlocked else "—"
            scolor = (26, 159, 255, 255) if unlocked else (100, 100, 100, 255)
            sw = dummy.textlength(status, font=font_name)
            draw.text((WIDTH - PADDING - sw - 10, y + 8), status, fill=scolor, font=font_name)

            y += ROW_H

    if total > limit:
        more = f"仅展示前 {limit} 个（共 {total} 个）"
        draw.text((PADDING, y + 4), more, fill=(140, 150, 160, 255), font=font_meta)

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
