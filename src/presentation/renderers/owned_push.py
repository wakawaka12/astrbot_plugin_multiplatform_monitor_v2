# -*- coding: utf-8 -*-
"""购游戏批量推送卡片 — 玩家头像 + 游戏封面，深色风格与 game_lib 同源。"""
from __future__ import annotations

import asyncio
import io
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image, ImageDraw

from ...shared.logging import format_exception, logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client
from .game_lib import (
    BG_BOTTOM,
    BG_TOP,
    CARD_BG,
    MARGIN,
    WIDTH,
    _font,
    _fetch_icon,
    _truncate,
)

ROW_H = 96
MAX_ROWS = 20
AVATAR_SIZE = 56
COVER_W = 168
COVER_H = 63  # header 约 460x215
_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}


def _round_mask(img: Image.Image, radius: int = 8) -> Image.Image:
    img = img.convert("RGBA")
    mask = Image.new("L", img.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, img.size[0] - 1, img.size[1] - 1), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def _fit_cover(img: Image.Image, w: int = COVER_W, h: int = COVER_H) -> Image.Image:
    """等比缩放并居中裁剪到目标尺寸。"""
    img = img.convert("RGBA")
    src_w, src_h = img.size
    if src_w <= 0 or src_h <= 0:
        return img.resize((w, h), Image.LANCZOS)
    scale = max(w / src_w, h / src_h)
    nw, nh = max(1, int(src_w * scale)), max(1, int(src_h * scale))
    img = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img.crop((left, top, left + w, top + h))


def _load_local_avatar(data_dir: Optional[str], sid: Any) -> Optional[Image.Image]:
    if not data_dir or not sid:
        return None
    for ext in (".jpg", ".png", ".jpeg"):
        path = os.path.join(data_dir, "avatars", f"{sid}{ext}")
        if os.path.exists(path):
            try:
                return Image.open(path).convert("RGBA")
            except Exception:
                continue
    return None


async def _fetch_avatar_img(
    sid: Any,
    avatar_url: Optional[str] = None,
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
) -> Optional[Image.Image]:
    # 本地缓存优先（状态监控已落盘）
    local = _load_local_avatar(data_dir, sid)
    if local is not None:
        return local
    if not avatar_url:
        return None
    try:
        async with shared_httpx_client(proxy=proxy, timeout=10, follow_redirects=False) as client:
            r = await client.get(avatar_url, headers=_UA)
            if r.status_code == 200 and r.content:
                img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                # 写缓存，供下次复用
                if data_dir and sid:
                    try:
                        adir = os.path.join(data_dir, "avatars")
                        os.makedirs(adir, exist_ok=True)
                        with open(os.path.join(adir, f"{sid}.jpg"), "wb") as f:
                            f.write(r.content)
                    except Exception:
                        pass
                return img
    except Exception as e:
        logger.debug(f"[owned_push] 头像下载失败 sid={sid}: {format_exception(e)}")
    return None


async def _fetch_cover_img(
    appid: Any,
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
) -> Optional[Image.Image]:
    """优先 header 横版封面，失败再试 library 竖版中心裁剪。"""
    if not appid:
        return None
    cache_dir = os.path.join(data_dir or "", "covers_h")
    cache_path = None
    if data_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            cache_path = os.path.join(cache_dir, f"{appid}.jpg")
            if os.path.exists(cache_path):
                try:
                    return Image.open(cache_path).convert("RGBA")
                except Exception:
                    pass
        except Exception:
            cache_path = None

    # 新版 Steam：哈希 store_item_assets 优先；无 assets 时先试现代路径再试旧 CDN
    try:
        from ...infrastructure.clients.steam import steam_cover_candidates
        candidates = steam_cover_candidates(appid)
    except Exception:
        candidates = [
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header.jpg",
            f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{appid}/header_schinese.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/header.jpg",
            f"https://cdn.akamai.steamstatic.com/steam/apps/{appid}/library_600x900.jpg",
        ]
    # 尽力从 GetItems 补一条哈希 URL（不传 key 也可）
    try:
        import json as _json
        input_json = {
            "ids": [{"appid": str(appid)}],
            "context": {"language": "schinese", "country_code": "CN"},
            "data_request": {"include_basic_info": True, "include_assets": True},
        }
        params = {"input_json": _json.dumps(input_json, ensure_ascii=False, separators=(",", ":"))}
        async with shared_httpx_client(proxy=proxy, timeout=8, follow_redirects=False) as client:
            gr = await client.get(
                "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                params=params,
                headers=_UA,
            )
        if gr.status_code == 200:
            from ...infrastructure.clients.steam import steam_store_asset_cover_urls
            items = (gr.json() or {}).get("response", {}).get("store_items") or []
            if items:
                hashed = steam_store_asset_cover_urls(str(appid), items[0].get("assets") or {})
                candidates = hashed + [c for c in candidates if c not in hashed]
    except Exception:
        pass
    async with shared_httpx_client(proxy=proxy, timeout=10, follow_redirects=False) as client:
        for url in candidates:
            try:
                r = await client.get(url, headers=_UA)
                if r.status_code == 200 and r.content:
                    img = Image.open(io.BytesIO(r.content)).convert("RGBA")
                    if cache_path:
                        try:
                            with open(cache_path, "wb") as f:
                                f.write(r.content)
                        except Exception:
                            pass
                    return img
            except Exception as e:
                logger.debug(f"[owned_push] 封面下载失败 {url}: {format_exception(e)}")
    return None


def _placeholder(size, radius=8, fill=(60, 70, 85, 255)) -> Image.Image:
    img = Image.new("RGBA", size, fill)
    return _round_mask(img, radius)


def render_owned_push_card(
    items: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    title: str = "本群新购入",
) -> Optional[bytes]:
    """items: [{sid, player_name, game_name, appid, avatar?, cover?, icon?}]"""
    if not items:
        return None
    n = min(len(items), MAX_ROWS)
    header_h = 88
    height = header_h + n * (ROW_H + 6) + MARGIN + 32
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

    players = []
    seen_p = set()
    for it in items:
        p = it.get("player_name") or "?"
        if p not in seen_p:
            seen_p.add(p)
            players.append(p)
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    subtitle = f"{datetime.now().strftime('%m-%d %H:%M')} · {len(players)} 人 · 共 {len(items)} 款"
    bbox2 = draw.textbbox((0, 0), subtitle, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 46), subtitle, font=font_meta, fill=(143, 152, 160, 255))

    y = header_h
    for idx, item in enumerate(items[:MAX_ROWS]):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        bar_color = (255, 200, 60, 255) if idx < 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 12, x0 + 10, y + ROW_H - 12), radius=3, fill=bar_color)

        # 右侧封面
        cover = item.get("cover")
        cover_x = x1 - COVER_W - 14
        cover_y = y + (ROW_H - COVER_H) // 2
        if cover is not None:
            try:
                c = _fit_cover(item["cover"], COVER_W, COVER_H)
                c = _round_mask(c, 8)
                img.alpha_composite(c, (cover_x, cover_y))
            except Exception:
                ph = _placeholder((COVER_W, COVER_H), 8)
                img.alpha_composite(ph, (cover_x, cover_y))
        else:
            ph = _placeholder((COVER_W, COVER_H), 8)
            img.alpha_composite(ph, (cover_x, cover_y))

        # 左侧头像
        avatar = item.get("avatar")
        av_x = x0 + 18
        av_y = y + (ROW_H - AVATAR_SIZE) // 2
        if avatar is not None:
            try:
                a = avatar.resize((AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
                a = _round_mask(a, 10)
                img.alpha_composite(a, (av_x, av_y))
            except Exception:
                ph = _placeholder((AVATAR_SIZE, AVATAR_SIZE), 10)
                img.alpha_composite(ph, (av_x, av_y))
        else:
            ph = _placeholder((AVATAR_SIZE, AVATAR_SIZE), 10)
            img.alpha_composite(ph, (av_x, av_y))

        # 中间文字
        text_x = av_x + AVATAR_SIZE + 14
        text_max = cover_x - text_x - 14
        player = _truncate(draw, str(item.get("player_name") or "?"), font_name, text_max)
        game = _truncate(draw, str(item.get("game_name") or "?"), font_meta, text_max)
        draw.text((text_x, y + 24), player, font=font_name, fill=(255, 255, 255, 255))
        draw.text((text_x, y + 52), game, font=font_meta, fill=(180, 220, 255, 255))

        # 小方图标叠在封面左下（若有）
        icon = item.get("icon")
        if icon is not None:
            try:
                icon_size = 22
                ic = icon.resize((icon_size, icon_size), Image.LANCZOS)
                ic = _round_mask(ic, 4)
                img.alpha_composite(ic, (cover_x - 8, cover_y + COVER_H - icon_size + 8))
            except Exception:
                pass

        y += ROW_H + 6

    if len(items) > MAX_ROWS:
        draw.text((MARGIN, height - 26), f"仅展示前 {MAX_ROWS} 条（共 {len(items)} 条）", font=font_meta, fill=(140, 150, 160, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def attach_icons(
    items: List[Dict[str, Any]],
    data_dir: Optional[str] = None,
    proxy: Optional[str] = None,
    avatar_urls: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """并发拉取头像、封面、小图标。"""
    if not items:
        return items
    avatar_urls = avatar_urls or {}

    async def _one(it: Dict[str, Any]):
        sid = it.get("sid")
        appid = it.get("appid")
        avatar, cover, icon = await asyncio.gather(
            _fetch_avatar_img(sid, avatar_urls.get(str(sid)), data_dir=data_dir, proxy=proxy),
            _fetch_cover_img(appid, data_dir=data_dir, proxy=proxy),
            _fetch_icon({"appid": appid, "name": it.get("game_name")}, data_dir=data_dir, proxy=proxy),
            return_exceptions=False,
        )
        it["avatar"] = avatar if isinstance(avatar, Image.Image) else None
        it["cover"] = cover if isinstance(cover, Image.Image) else None
        it["icon"] = icon if isinstance(icon, Image.Image) else None
        return it

    try:
        return list(await asyncio.gather(*(_one(it) for it in items)))
    except Exception as e:
        logger.warning(f"[owned_push] 资源预取失败: {e}")
        return items


def build_text_summary(items: List[Dict[str, Any]]) -> str:
    if not items:
        return ""
    order: List[str] = []
    mapping: Dict[str, List[str]] = {}
    for it in items:
        p = str(it.get("player_name") or "?")
        g = str(it.get("game_name") or "?")
        if p not in mapping:
            mapping[p] = []
            order.append(p)
        if g not in mapping[p]:
            mapping[p].append(g)
    lines = [f"📦 本批新购入 {len(items)} 款（{len(order)} 人）"]
    for p in order[:8]:
        games = "、".join(mapping[p][:4])
        more = f" 等{len(mapping[p])}款" if len(mapping[p]) > 4 else ""
        lines.append(f"· {p}：{games}{more}")
    if len(order) > 8:
        lines.append(f"· …等 {len(order)} 人")
    return "\n".join(lines)
