# -*- coding: utf-8 -*-
"""Steam 游戏库卡片渲染 — 深色风格，与 rank/list 同源"""
import io
import json
import os
import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx
from PIL import Image, ImageDraw

from ...shared.fonts import load_truetype
from ...shared.logging import format_exception, logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client

BG_TOP = (44, 62, 80)
BG_BOTTOM = (24, 32, 44)
CARD_BG = (38, 44, 56)
WIDTH = 680
MARGIN = 18
ROW_H = 64
ICON_SIZE = 44


def _font(path, size, bold=False):
    name = "NotoSansHans-Medium.otf" if bold else "NotoSansHans-Regular.otf"
    extra = ()
    if path:
        extra = (str(path).replace("Regular", "Medium") if bold else path,)
    return load_truetype(name, size, fallbacks=extra)


def _fmt_hours(minutes):
    h = minutes / 60.0
    if h >= 1:
        return f"{h:.1f}h"
    return f"{int(minutes)}min"


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


_ICON_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    )
}
_ICON_CACHE_DIR = "game_icons"
_ICON_CACHE_TTL = 30 * 24 * 3600


def _square_crop(img: Image.Image) -> Image.Image:
    """居中裁成正方形，适配商店横/竖版封面兜底。"""
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def _icon_cache_path(data_dir: Optional[str], appid: Any) -> Optional[str]:
    if not data_dir or not appid:
        return None
    cache_dir = os.path.join(data_dir, _ICON_CACHE_DIR)
    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError:
        return None
    return os.path.join(cache_dir, f"{appid}.png")


def _load_icon_cache(path: Optional[str]) -> Optional[Image.Image]:
    if not path or not os.path.exists(path):
        return None
    try:
        if _ICON_CACHE_TTL and (datetime.now().timestamp() - os.path.getmtime(path)) > _ICON_CACHE_TTL:
            return None
        return Image.open(path).convert("RGBA")
    except Exception:
        return None


def _save_icon_cache(path: Optional[str], img: Optional[Image.Image]) -> None:
    if not path or img is None:
        return
    try:
        img.save(path, format="PNG")
    except Exception as e:
        logger.debug(f"[game_lib] 图标缓存写入失败 {path}: {e}")


def _build_icon_candidates(game: Dict[str, Any]) -> List[str]:
    appid = game.get("appid")
    icon_hash = game.get("icon_hash") or ""
    icon_url = game.get("icon_url") or ""
    candidates: List[str] = []

    def add(url: str):
        if url and url not in candidates:
            candidates.append(url)

    # 优先：GetItems/appdetails 已解析出的新版哈希封面
    cover_urls = game.get("cover_urls") or []
    if isinstance(cover_urls, (list, tuple)):
        for u in cover_urls:
            add(str(u or ""))
    add(game.get("cover_url") or "")

    if icon_hash and appid:
        for host in (
            "cdn.akamai.steamstatic.com",
            "cdn.cloudflare.steamstatic.com",
            "shared.akamai.steamstatic.com",
            "steamcdn-a.akamaihd.net",
        ):
            add(f"https://{host}/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg")
    add(icon_url)
    if icon_url:
        add(icon_url.replace("media.steamstatic.com", "cdn.akamai.steamstatic.com"))
    # 商店资源不依赖 icon hash，可稳定下载（服务器网络可达）
    # 新版 Steam：无哈希路径对很多新游戏 404，但旧游戏仍可用；哈希路径优先
    if appid:
        for host in (
            "shared.akamai.steamstatic.com/store_item_assets",
            "shared.cloudflare.steamstatic.com/store_item_assets",
            "cdn.akamai.steamstatic.com",
            "cdn.cloudflare.steamstatic.com",
        ):
            prefix = f"https://{host}"
            if "store_item_assets" in host:
                add(f"{prefix}/steam/apps/{appid}/header.jpg")
                add(f"{prefix}/steam/apps/{appid}/capsule_231x87.jpg")
                add(f"{prefix}/steam/apps/{appid}/library_600x900.jpg")
            else:
                add(f"{prefix}/steam/apps/{appid}/library_600x900.jpg")
                add(f"{prefix}/steam/apps/{appid}/header.jpg")
                add(f"{prefix}/steam/apps/{appid}/capsule_231x87.jpg")
    return candidates


async def _download_image(url: str, client: httpx.AsyncClient) -> Optional[Image.Image]:
    try:
        r = await client.get(url, headers=_ICON_UA)
        if r.status_code == 200 and r.content:
            ctype = (r.headers.get("content-type") or "").lower()
            if ctype and "image" not in ctype and "octet-stream" not in ctype:
                return None
            return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception as e:
        logger.debug(f"[game_lib] 图标下载失败 {url}: {format_exception(e)}")
    return None


async def _fetch_icon(game: Dict[str, Any], data_dir: Optional[str] = None, proxy: Optional[str] = None):
    appid = game.get("appid")
    cache_path = _icon_cache_path(data_dir, appid)
    cached = _load_icon_cache(cache_path)
    if cached is not None:
        return _square_crop(cached).resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)

    candidates = _build_icon_candidates(game)
    if not candidates:
        return None

    async with shared_httpx_client(proxy=proxy, timeout=10, follow_redirects=False) as client:
        for url in candidates:
            img = await _download_image(url, client)
            if img is None:
                continue
            square = _square_crop(img)
            _save_icon_cache(cache_path, square)
            return square.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)

    # 兜底：从 GetItems 拉 assets（新版游戏旧 CDN 路径常 404）
    if appid:
        try:
            from ...infrastructure.clients.steam import steam_store_asset_cover_urls
            input_json = {
                "ids": [{"appid": str(appid)}],
                "context": {"language": "schinese", "country_code": "CN"},
                "data_request": {"include_basic_info": True, "include_assets": True},
            }
            params = {"input_json": json.dumps(input_json, ensure_ascii=False, separators=(",", ":"))}
            async with shared_httpx_client(proxy=proxy, timeout=10, follow_redirects=False) as client:
                r = await client.get(
                    "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                    params=params,
                    headers=_ICON_UA,
                )
                if r.status_code == 200:
                    items = (r.json() or {}).get("response", {}).get("store_items") or []
                    if items:
                        extra = steam_store_asset_cover_urls(str(appid), items[0].get("assets") or {})
                        for url in extra:
                            img = await _download_image(url, client)
                            if img is None:
                                continue
                            square = _square_crop(img)
                            _save_icon_cache(cache_path, square)
                            return square.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
        except Exception as e:
            logger.debug(f"[game_lib] GetItems 封面兜底失败 appid={appid}: {format_exception(e)}")

    logger.warning(f"[game_lib] 游戏图标全部候选失败 appid={appid} name={game.get('name')!r}")
    return None


async def render_game_lib_image(
    data_dir: str,
    player_name: str,
    games: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    proxy: Optional[str] = None,
    limit: int = 15,
) -> Optional[bytes]:
    """渲染游戏库卡片，返回 PNG bytes。

    games: [{"appid", "name", "playtime_minutes", "playtime_2weeks", "last_played", "icon_url"}]
    按总时长降序，取前 limit 条。
    """
    if not games:
        return None
    games = sorted(games, key=lambda g: g.get("playtime_minutes", 0), reverse=True)[:limit]
    n = len(games)
    total_min = sum(g.get("playtime_minutes", 0) for g in games)
    header_h = 90
    height = header_h + n * (ROW_H + 6) + MARGIN + 40

    img = Image.new("RGBA", (WIDTH, height), BG_TOP)
    draw = ImageDraw.Draw(img)
    for y in range(height):
        ratio = y / max(1, height - 1)
        r = int(BG_TOP[0] * (1 - ratio) + BG_BOTTOM[0] * ratio)
        g = int(BG_TOP[1] * (1 - ratio) + BG_BOTTOM[1] * ratio)
        b = int(BG_TOP[2] * (1 - ratio) + BG_BOTTOM[2] * ratio)
        draw.line([(0, y), (WIDTH, y)], fill=(r, g, b, 255))

    font_title = _font(font_path, 26, bold=True)
    font_name = _font(font_path, 16, bold=True)
    font_time = _font(font_path, 13)
    font_meta = _font(font_path, 12)

    # 标题
    title = f"{player_name} 的游戏库"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 14), title, font=font_title, fill=(255, 255, 255, 255))
    now_str = datetime.now().strftime("%Y/%m/%d")
    subtitle = f"{now_str} · 共 {len(games)} 款 · 合计 {_fmt_hours(total_min)}"
    bbox2 = draw.textbbox((0, 0), subtitle, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 50), subtitle, font=font_meta, fill=(143, 152, 160, 255))

    # 并发拉图标（多 CDN + 商店兜底 + 本地缓存）
    icons = await asyncio.gather(*(
        _fetch_icon(g, data_dir=data_dir, proxy=proxy) for g in games
    ))

    y = header_h
    for idx, game in enumerate(games):
        x0 = MARGIN
        x1 = WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        # 左侧色条
        bar_color = (102, 192, 244, 255) if idx < 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 12, x0 + 10, y + ROW_H - 12), radius=3, fill=bar_color)

        # 图标
        icon = icons[idx]
        icon_x = x0 + 20
        icon_y = y + (ROW_H - ICON_SIZE) // 2
        if icon is not None:
            icon = icon.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            mask = Image.new("L", (ICON_SIZE, ICON_SIZE), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ICON_SIZE, ICON_SIZE), radius=6, fill=255)
            img.paste(icon, (icon_x, icon_y), mask)
        else:
            draw.rounded_rectangle((icon_x, icon_y, icon_x + ICON_SIZE, icon_y + ICON_SIZE), radius=6, fill=(60, 70, 85, 255))

        # 游戏名
        name_x = icon_x + ICON_SIZE + 14
        right_x = x1 - 110
        name = _truncate(draw, game.get("name", "?"), font_name, right_x - name_x - 8)
        draw.text((name_x, y + 14), name, font=font_name, fill=(255, 255, 255, 255))

        # 总时长
        total_str = _fmt_hours(game.get("playtime_minutes", 0))
        draw.text((name_x, y + 38), f"总时长 {total_str}", font=font_time, fill=(180, 220, 255, 255))

        # 右侧：两周时长 + 上次游玩
        w2 = game.get("playtime_2weeks", 0)
        if w2 > 0:
            w2_str = f"两周 {_fmt_hours(w2)}"
            w2_bbox = draw.textbbox((0, 0), w2_str, font=font_time)
            draw.text((x1 - (w2_bbox[2] - w2_bbox[0]) - 16, y + 14), w2_str, font=font_time, fill=(255, 200, 60, 255))
        last = game.get("last_played", 0)
        if last > 0:
            try:
                dt = datetime.fromtimestamp(last)
                last_str = f"上次 {dt.strftime('%m-%d')}"
                last_bbox = draw.textbbox((0, 0), last_str, font=font_meta)
                draw.text((x1 - (last_bbox[2] - last_bbox[0]) - 16, y + 38), last_str, font=font_meta, fill=(140, 150, 160, 255))
            except Exception:
                pass

        y += ROW_H + 6

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()
