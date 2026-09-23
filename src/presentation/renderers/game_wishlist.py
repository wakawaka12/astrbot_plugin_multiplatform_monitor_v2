# -*- coding: utf-8 -*-
"""公开愿望单：全量分页卡片（每页 20 条 + 封面）。"""
from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

from PIL import Image, ImageDraw

from ...shared.logging import logger
from .game_lib import (
    BG_BOTTOM,
    BG_TOP,
    MARGIN,
    WIDTH,
    _fetch_icon,
    _font,
    _truncate,
)

ROW_H = 52
COVER = 40
PAGE_SIZE = 20
MAX_PAGES = 12  # 安全上限：240 条


def _round(img: Image.Image, radius: int = 6) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


async def attach_wishlist_covers(
    items: List[Dict[str, Any]],
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
) -> List[Dict[str, Any]]:
    if not items:
        return items
    try:
        icons = await asyncio.gather(*(
            _fetch_icon(
                {
                    "appid": it.get("appid"),
                    "name": it.get("name"),
                    "cover_url": it.get("cover_url"),
                    "cover_urls": it.get("cover_urls"),
                },
                data_dir=data_dir,
                proxy=proxy,
            )
            for it in items
        ))
        for it, icon in zip(items, icons):
            it["cover"] = icon
    except Exception as e:
        logger.warning(f"[wish] 封面预取失败: {e}")
        for it in items:
            it.setdefault("cover", None)
    return items


def render_wishlist_page(
    player_name: str,
    items: List[Dict[str, Any]],
    *,
    page: int = 1,
    total_items: int = 0,
    font_path: Optional[str] = None,
    total_pages: int = 1,
) -> Optional[bytes]:
    """渲染某一页愿望单（items 已是本页 20 条，可含 cover）。"""
    n = len(items)
    if n <= 0:
        return None
    header_h = 92
    footer_h = 42
    height = header_h + n * (ROW_H + 4) + footer_h + 12
    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 22, bold=True)
    font_name = _font(font_path, 14)
    font_meta = _font(font_path, 12)

    total = total_items or (page - 1) * PAGE_SIZE + n
    title = f"{player_name} 的愿望单"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    start_i = (page - 1) * PAGE_SIZE + 1
    end_i = start_i + n - 1
    pages = max(total_pages, 1)
    sub = f"{datetime.now().strftime('%Y/%m/%d')} · 公开数据 {total} 条 · 第 {page}/{pages} 页（{start_i}–{end_i}）"
    bbox2 = draw.textbbox((0, 0), sub, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 44), sub, font=font_meta, fill=(143, 152, 160, 255))

    y = header_h
    for i, item in enumerate(items):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=8, fill=(38, 44, 56, 230))
        global_idx = (page - 1) * PAGE_SIZE + i + 1
        bar = (255, 200, 60, 255) if global_idx <= 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 10, x0 + 10, y + ROW_H - 10), radius=2, fill=bar)

        cover = item.get("cover")
        cx = x0 + 16
        cy = y + (ROW_H - COVER) // 2
        if cover is not None:
            try:
                c = _round(cover.resize((COVER, COVER), Image.LANCZOS), 6)
                img.alpha_composite(c, (cx, cy))
            except Exception:
                draw.rounded_rectangle((cx, cy, cx + COVER, cy + COVER), radius=6, fill=(60, 70, 85, 255))
        else:
            draw.rounded_rectangle((cx, cy, cx + COVER, cy + COVER), radius=6, fill=(60, 70, 85, 255))

        num = f"{global_idx:03d}"
        draw.text((x0 + COVER + 24, y + 18), num, font=font_meta, fill=(140, 150, 160, 255))
        name = item.get("name") or f"appid {item.get('appid')}"
        nx = x0 + COVER + 62
        maxw = x1 - nx - 12
        draw.text((nx, y + 16), _truncate(draw, str(name), font_name, maxw), font=font_name, fill=(220, 228, 236, 255))
        y += ROW_H + 4

    note = "公开愿望单 SSR · 可能少于客户端计数（成人向/下架可能未包含）"
    if page < pages:
        note += " · 见下一页"
    draw.text((MARGIN, height - 26), note, font=font_meta, fill=(140, 150, 160, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


def render_wishlist_pages(
    player_name: str,
    items: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    page_size: int = PAGE_SIZE,
) -> List[bytes]:
    """全量分页渲染，返回 PNG bytes 列表。"""
    if not items:
        return []
    size = max(1, int(page_size or PAGE_SIZE))
    total = len(items)
    pages = (total + size - 1) // size
    # 兼容 render_wishlist_page 里的 PAGE_SIZE 偏移：仅 page_size==20 时用全局序号
    outs: List[bytes] = []
    if size != PAGE_SIZE:
        # 非 20 时按块切片，页码仍按 20 的逻辑会错；这里统一用临时切片函数
        for pi in range(min(pages, MAX_PAGES)):
            chunk = items[pi * size:(pi + 1) * size]
            png = _render_chunk(player_name, chunk, pi + 1, total, pages, font_path, size)
            if png:
                outs.append(png)
        return outs
    for pi in range(min(pages, MAX_PAGES)):
        chunk = items[pi * PAGE_SIZE:(pi + 1) * PAGE_SIZE]
        png = render_wishlist_page(
            player_name,
            chunk,
            page=pi + 1,
            total_items=total,
            font_path=font_path,
            total_pages=pages,
        )
        if png:
            outs.append(png)
    return outs


def _render_chunk(player_name, chunk, page, total, pages, font_path, size):
    n = len(chunk)
    if n <= 0:
        return None
    header_h = 92
    footer_h = 42
    height = header_h + n * (ROW_H + 4) + footer_h + 12
    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))
    font_title = _font(font_path, 22, bold=True)
    font_name = _font(font_path, 14)
    font_meta = _font(font_path, 12)
    title = f"{player_name} 的愿望单"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    start = (page - 1) * size + 1
    end = start + n - 1
    sub = f"{datetime.now().strftime('%Y/%m/%d')} · 公开数据 {total} 条 · 第 {page}/{pages} 页（{start}–{end}）"
    bbox2 = draw.textbbox((0, 0), sub, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 44), sub, font=font_meta, fill=(143, 152, 160, 255))
    y = header_h
    for i, item in enumerate(chunk):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=8, fill=(38, 44, 56, 230))
        gi = (page - 1) * size + i + 1
        bar = (255, 200, 60, 255) if gi <= 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 10, x0 + 10, y + ROW_H - 10), radius=2, fill=bar)
        cover = item.get("cover")
        cx = x0 + 16
        cy = y + (ROW_H - COVER) // 2
        if cover is not None:
            try:
                c = _round(cover.resize((COVER, COVER), Image.LANCZOS), 6)
                img.alpha_composite(c, (cx, cy))
            except Exception:
                draw.rounded_rectangle((cx, cy, cx + COVER, cy + COVER), radius=6, fill=(60, 70, 85, 255))
        else:
            draw.rounded_rectangle((cx, cy, cx + COVER, cy + COVER), radius=6, fill=(60, 70, 85, 255))
        num = f"{gi:03d}"
        draw.text((x0 + COVER + 24, y + 18), num, font=font_meta, fill=(140, 150, 160, 255))
        name = item.get("name") or f"appid {item.get('appid')}"
        nx = x0 + COVER + 62
        maxw = x1 - nx - 12
        draw.text((nx, y + 16), _truncate(draw, str(name), font_name, maxw), font=font_name, fill=(220, 228, 236, 255))
        y += ROW_H + 4
    note = "公开愿望单 SSR · 可能少于客户端计数（成人向/下架可能未包含）"
    if page < pages:
        note += " · 见下一页"
    draw.text((MARGIN, height - 26), note, font=font_meta, fill=(140, 150, 160, 255))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
