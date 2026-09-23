# -*- coding: utf-8 -*-
"""用户「全成就」游戏列表卡片。"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw

from ...shared.logging import logger
from .game_lib import (
    BG_BOTTOM,
    BG_TOP,
    CARD_BG,
    MARGIN,
    WIDTH,
    _fetch_icon,
    _font,
    _fmt_hours,
    _truncate,
)

ROW_H = 58
ICON = 40


def _round(img: Image.Image, radius: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


async def render_perfect_games_image(
    player_name: str,
    games: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    *,
    checked: int = 0,
    owned_count: int = 0,
    perfect_hint: Optional[int] = None,
    limit: int = 15,
) -> Optional[bytes]:
    """渲染全成就游戏列表卡片 PNG bytes。"""
    games = list(games or [])[: max(1, int(limit or 15))]
    if not games:
        return None
    n = len(games)
    total_min = sum(int(g.get("playtime_minutes") or 0) for g in games)
    header_h = 96
    height = header_h + n * (ROW_H + 6) + MARGIN + 42

    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 24, bold=True)
    font_name = _font(font_path, 15, bold=True)
    font_meta = _font(font_path, 12)

    title = f"{player_name} · 全成就游戏"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 14), title, font=font_title, fill=(255, 255, 255, 255))

    hint_s = ""
    if perfect_hint is not None:
        hint_s = f" · 社区展示 {perfect_hint} 款"
    sub = (
        f"{datetime.now().strftime('%Y/%m/%d')} · 本次确认 {n} 款"
        f"{hint_s} · 扫描 {checked}/{owned_count} · 合计 {_fmt_hours(total_min)}"
    )
    bbox2 = draw.textbbox((0, 0), sub, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 52), sub, font=font_meta, fill=(143, 152, 160, 255))

    icons = await _fetch_all_icons(games, data_dir=data_dir, proxy=proxy)

    y = header_h
    for idx, game in enumerate(games):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        bar = (255, 200, 60, 255) if idx < 3 else (102, 192, 244, 255)
        draw.rounded_rectangle((x0 + 6, y + 12, x0 + 10, y + ROW_H - 12), radius=3, fill=bar)

        icon = icons[idx] if idx < len(icons) else None
        cx, cy = x0 + 20, y + (ROW_H - ICON) // 2
        if icon is not None:
            try:
                c = _round(icon.resize((ICON, ICON), Image.LANCZOS), 6)
                img.alpha_composite(c, (cx, cy))
            except Exception:
                draw.rounded_rectangle((cx, cy, cx + ICON, cy + ICON), radius=6, fill=(60, 70, 85, 255))
        else:
            draw.rounded_rectangle((cx, cy, cx + ICON, cy + ICON), radius=6, fill=(60, 70, 85, 255))

        num = f"{idx + 1:02d}"
        draw.text((x0 + ICON + 28, y + 12), num, font=font_meta, fill=(140, 150, 160, 255))
        name = game.get("name") or game.get("appid")
        nx = x0 + ICON + 64
        maxw = x1 - nx - 120
        draw.text((nx, y + 10), _truncate(draw, str(name), font_name, maxw), font=font_name, fill=(220, 228, 236, 255))

        ach_n = int(game.get("achievement_count") or 0)
        hours = _fmt_hours(int(game.get("playtime_minutes") or 0))
        meta = f"100% 成就 {ach_n} · {hours}"
        draw.text((nx, y + 34), meta, font=font_meta, fill=(255, 200, 80, 255))

        # 右侧 100% 徽章
        badge = "100%"
        bf = _font(font_path, 14, bold=True)
        bb = draw.textbbox((0, 0), badge, font=bf)
        bw = bb[2] - bb[0] + 18
        bx = x1 - bw - 12
        by = y + (ROW_H - 28) // 2
        draw.rounded_rectangle((bx, by, bx + bw, by + 28), radius=14, fill=(22, 163, 110, 230))
        draw.text((bx + 9, by + 5), badge, font=bf, fill=(255, 255, 255, 255))
        y += ROW_H + 6

    note = "全成就 = 该游戏成就列表全部解锁 · 社区数可能因隐私/未扫描游戏略有差异"
    draw.text((MARGIN, height - 26), note, font=font_meta, fill=(140, 150, 160, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def render_perfect_games_pages(
    player_name: str,
    games: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    *,
    checked: int = 0,
    owned_count: int = 0,
    perfect_hint: Optional[int] = None,
    page_size: int = 12,
    progress_cb=None,
    cancel_event=None,
) -> List[bytes]:
    """全库全成就：分页渲染，返回多页 PNG bytes。

    progress_cb: 可选 async/sync 回调 (phase, done, total, pct)
      phase: "icons" | "pages"
    cancel_event: set 后停止出图
    """
    games = list(games or [])
    if not games:
        return []
    size = max(1, int(page_size or 12))
    total = len(games)
    pages_n = (total + size - 1) // size
    outs: List[bytes] = []

    def _cancelled():
        return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()

    async def _prog(phase, done, tot, pct):
        if not progress_cb:
            return
        try:
            r = progress_cb(phase, done, tot, pct)
            if asyncio.iscoroutine(r):
                await r
        except Exception as e:
            logger.debug(f"[perfect_card] progress_cb: {e}")

    if _cancelled():
        return []
    icons_all = await _fetch_all_icons(
        games, data_dir=data_dir, proxy=proxy, progress_cb=_prog if progress_cb else None,
        cancel_event=cancel_event,
    )
    for pi in range(pages_n):
        if _cancelled():
            break
        chunk = games[pi * size:(pi + 1) * size]
        icon_chunk = icons_all[pi * size:(pi + 1) * size]
        png = await _render_perfect_page(
            player_name,
            chunk,
            icon_chunk,
            font_path=font_path,
            page=pi + 1,
            total_pages=pages_n,
            global_offset=pi * size,
            total_games=total,
            checked=checked,
            owned_count=owned_count,
            perfect_hint=perfect_hint,
        )
        if png:
            outs.append(png)
        await _prog("pages", pi + 1, pages_n, min(100, int((pi + 1) * 100 / max(pages_n, 1))))
    return outs


async def _render_perfect_page(
    player_name,
    games,
    icons,
    font_path=None,
    page=1,
    total_pages=1,
    global_offset=0,
    total_games=0,
    checked=0,
    owned_count=0,
    perfect_hint=None,
):
    n = len(games)
    if n <= 0:
        return None
    header_h = 96
    height = header_h + n * (ROW_H + 6) + MARGIN + 42
    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 22, bold=True)
    font_name = _font(font_path, 15, bold=True)
    font_meta = _font(font_path, 12)

    start = global_offset + 1
    end = global_offset + n
    title = f"{player_name} · 全成就游戏"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    hint_s = f" · 社区 {perfect_hint} 款" if perfect_hint is not None else ""
    sub = (
        f"{datetime.now().strftime('%Y/%m/%d')} · 共 {total_games} 款{hint_s}"
        f" · 第 {page}/{total_pages} 页（{start}–{end}） · 扫描 {checked}/{owned_count}"
    )
    bbox2 = draw.textbbox((0, 0), sub, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 50), sub, font=font_meta, fill=(143, 152, 160, 255))

    y = header_h
    for idx, game in enumerate(games):
        x0, x1 = MARGIN, WIDTH - MARGIN
        gi = global_offset + idx + 1
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        bar = (255, 200, 60, 255) if gi <= 3 else (102, 192, 244, 255)
        draw.rounded_rectangle((x0 + 6, y + 12, x0 + 10, y + ROW_H - 12), radius=3, fill=bar)

        icon = icons[idx] if idx < len(icons) else None
        cx, cy = x0 + 20, y + (ROW_H - ICON) // 2
        if icon is not None:
            try:
                c = _round(icon.resize((ICON, ICON), Image.LANCZOS), 6)
                img.alpha_composite(c, (cx, cy))
            except Exception:
                draw.rounded_rectangle((cx, cy, cx + ICON, cy + ICON), radius=6, fill=(60, 70, 85, 255))
        else:
            draw.rounded_rectangle((cx, cy, cx + ICON, cy + ICON), radius=6, fill=(60, 70, 85, 255))

        num = f"{gi:03d}"
        draw.text((x0 + ICON + 28, y + 12), num, font=font_meta, fill=(140, 150, 160, 255))
        name = game.get("name") or game.get("appid")
        nx = x0 + ICON + 64
        maxw = x1 - nx - 120
        draw.text((nx, y + 10), _truncate(draw, str(name), font_name, maxw), font=font_name, fill=(220, 228, 236, 255))
        ach_n = int(game.get("achievement_count") or 0)
        hours = _fmt_hours(int(game.get("playtime_minutes") or 0))
        draw.text((nx, y + 34), f"100% 成就 {ach_n} · {hours}", font=font_meta, fill=(255, 200, 80, 255))

        badge = "100%"
        bf = _font(font_path, 14, bold=True)
        bb = draw.textbbox((0, 0), badge, font=bf)
        bw = bb[2] - bb[0] + 18
        bx = x1 - bw - 12
        by = y + (ROW_H - 28) // 2
        draw.rounded_rectangle((bx, by, bx + bw, by + 28), radius=14, fill=(22, 163, 110, 230))
        draw.text((bx + 9, by + 5), badge, font=bf, fill=(255, 255, 255, 255))
        y += ROW_H + 6

    note = f"全成就游戏 · 第 {page}/{total_pages} 页 · 按游玩时长排序"
    draw.text((MARGIN, height - 26), note, font=font_meta, fill=(140, 150, 160, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def _fetch_all_icons(games, data_dir=None, proxy=None, progress_cb=None, cancel_event=None):
    games = list(games or [])
    if not games:
        return []
    sem = asyncio.Semaphore(8)
    state = {"done": 0}
    total = len(games)
    lock = asyncio.Lock()

    def _cancelled():
        return cancel_event is not None and getattr(cancel_event, "is_set", lambda: False)()

    async def _one(g):
        if _cancelled():
            return None
        async with sem:
            if _cancelled():
                return None
            try:
                img = await _fetch_icon(g, data_dir=data_dir, proxy=proxy)
            except Exception as e:
                logger.debug(f"[perfect_card] 图标失败 {g.get('appid')}: {e}")
                img = None
        async with lock:
            state["done"] += 1
            done = state["done"]
            # 进度：约每 1/3 一次
            if progress_cb and (done % max(1, total // 3) == 0 or done == total):
                pct = min(100, int(done * 100 / total))
                try:
                    r = progress_cb("icons", done, total, pct)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception as e:
                    logger.debug(f"[perfect_card] icons progress: {e}")
        return img

    return list(await asyncio.gather(*(_one(g) for g in games)))
