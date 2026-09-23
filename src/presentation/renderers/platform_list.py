"""全平台状态列表渲染器 —— 四栏布局（Steam / PS / Switch / Xbox）。

展示全部玩家（含离线）：游玩中显示游戏名+时长，在线显示状态，
离线显示「上次在线 X 小时前」。分栏标题条 + 玩家卡片（圆形头像 + 名字 + 游戏名/时长 + 封面图），
空平台显示「暂无玩家」。

调用方构造 user_list（与现有 steam alllist 相同结构），sid 可从
psn:/xbox:/nso: 前缀识别平台。
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import time
from typing import Any, Callable, Dict, List, Optional

import httpx
from PIL import Image, ImageDraw, ImageFont

from ...shared.network import httpx_client_kwargs, shared_httpx_view

PLATFORM_META = {
    "steam": {"label": "Steam", "color": (23, 78, 166)},
    "psn": {"label": "PS", "color": (0, 90, 200)},
    "nso": {"label": "Switch", "color": (227, 37, 43)},
    "xbox": {"label": "Xbox", "color": (16, 124, 16)},
}

BG = (15, 20, 28)
CARD_BG = (26, 33, 44)
CARD_BG_OFF = (20, 25, 33)
CARD_BORDER = (40, 50, 64)
TITLE_TEXT = (235, 240, 245)
NAME_TEXT = (228, 233, 238)
NAME_TEXT_OFF = (150, 158, 172)
GAME_TEXT = (120, 150, 190)
GAME_TEXT_OFF = (100, 110, 128)
META_TEXT = (90, 105, 125)
EMPTY_TEXT = (110, 120, 135)
STATUS_TEXT = {"online": "在线", "busy": "忙碌", "away": "离开", "snooze": "打盹"}
STATUS_ICON = {"playing": "●", "online": "●", "busy": "●", "away": "●", "snooze": "●"}


def _platform_of(sid: str) -> str:
    if ":" in sid:
        p = sid.split(":", 1)[0]
        if p in ("psn", "xbox", "nso"):
            return p
    return "steam"


async def _fetch_image(
    url: Optional[str],
    session: httpx.AsyncClient,
    cache_path: Optional[str] = None,
) -> Optional[Image.Image]:
    """拉取图片；cache_path 存在时优先读本地缓存（与旧 Steam 渲染一致）。"""
    if not url:
        return None
    if cache_path and os.path.exists(cache_path):
        try:
            img = Image.open(cache_path)
            img.load()
            return img.convert("RGB")
        except Exception:
            pass
    try:
        resp = await session.get(url, timeout=12)
        if resp.status_code == 200 and resp.content:
            img = Image.open(io.BytesIO(resp.content))
            img.load()
            if cache_path:
                try:
                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                    img.save(cache_path)
                except Exception:
                    pass
            return img.convert("RGB")
    except Exception:
        pass
    return None


def _round_avatar(img: Image.Image, size: int) -> Image.Image:
    """方形头像（保留 Alpha 通道，透明区域透出底色，与 steam list 一致）。"""
    return img.resize((size, size), Image.LANCZOS)


def _fit_cover(img: Image.Image, w: int, h: int) -> Image.Image:
    scale = max(w / img.width, h / img.height)
    img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    left = (img.width - w) // 2
    top = (img.height - h) // 2
    return img.crop((left, top, left + w, top + h))


def _display_name(u: Dict[str, Any]) -> str:
    """玩家展示名：绑定的备注名优先，其次去掉平台前缀的 ID。"""
    name = u.get("name") or u.get("sid") or "?"
    name = str(name)
    for p in ("psn:", "xbox:", "nso:"):
        if name.startswith(p):
            name = name[len(p):]
    return name


def _truncate(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text + "…"


def _load_font(font_path: Optional[str], size: int) -> ImageFont.FreeTypeFont:
    if font_path and os.path.exists(font_path):
        try:
            return ImageFont.truetype(font_path, size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc", size)
    except Exception:
        return ImageFont.load_default()


async def render_platform_list_image(
    user_list: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    proxy: Optional[str] = None,
    cover_resolver: Optional[Callable[..., Any]] = None,
    session: Optional[httpx.AsyncClient] = None,
    data_dir: Optional[str] = None,
    **kwargs,
) -> Optional[str]:
    """渲染四平台玩家状态列表，返回图片文件路径（失败返回 None）。
    data_dir：头像本地缓存目录（data/steam_status_monitor），复用旧渲染缓存。"""
    COL_W = 430
    COL_GAP = 14
    CARD_H = 120
    CARD_GAP = 12
    PAD = 24
    HEADER_H = 108
    PLATFORM_ORDER = ["steam", "psn", "nso", "xbox"]

    # 全量展示（含离线）；去重（同一玩家多群只显示一次）
    seen = set()
    deduped = []
    for u in user_list:
        key = u.get("sid", "") or id(u)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(u)

    by_platform: Dict[str, List[Dict[str, Any]]] = {p: [] for p in PLATFORM_ORDER}
    for u in deduped:
        by_platform[_platform_of(u.get("sid", ""))].append(u)

    max_rows = max((len(v) for v in by_platform.values()), default=1)
    total_h = HEADER_H + max_rows * (CARD_H + CARD_GAP) + PAD + 20
    width = PAD * 2 + len(PLATFORM_ORDER) * COL_W + (len(PLATFORM_ORDER) - 1) * COL_GAP
    canvas = Image.new("RGB", (width, total_h), BG)
    draw = ImageDraw.Draw(canvas)

    font_title = _load_font(font_path, 30)
    font_col = _load_font(font_path, 26)
    font_name = _load_font(font_path, 22)
    font_game = _load_font(font_path, 18)
    font_meta = _load_font(font_path, 16)
    font_empty = _load_font(font_path, 20)

    own_session = session is None
    if own_session:
        # 池化视图：finally 中不关闭底层连接
        session = await shared_httpx_view(proxy, timeout=15.0, follow_redirects=True)

    try:
        total = len(deduped)
        playing = sum(1 for u in deduped if u.get("status") == "playing")
        online = sum(1 for u in deduped if u.get("status") in ("online", "busy", "away", "snooze"))
        draw.text((PAD, 20), "本群在玩 / 在线", font=font_title, fill=TITLE_TEXT)
        draw.text(
            (PAD + 270, 32),
            f"共 {total} 位（在玩 {playing} · 在线 {online}）",
            font=font_game, fill=GAME_TEXT,
        )

        # 分栏
        for ci, platform in enumerate(PLATFORM_ORDER):
            x0 = PAD + ci * (COL_W + COL_GAP)
            meta = PLATFORM_META[platform]
            label = meta["label"]
            draw.rounded_rectangle(
                (x0, HEADER_H - 46, x0 + COL_W, HEADER_H - 8), radius=12, fill=meta["color"],
            )
            text_w = draw.textlength(label, font=font_col)
            draw.text((x0 + (COL_W - text_w) / 2, HEADER_H - 40), label, font=font_col, fill=(255, 255, 255))
            rows = by_platform[platform]
            if not rows:
                cy = HEADER_H + 46
                draw.text((x0 + COL_W / 2, cy), "暂无玩家", font=font_empty, fill=EMPTY_TEXT, anchor="mm")
                continue
            for ri, u in enumerate(rows):
                cy = HEADER_H + ri * (CARD_H + CARD_GAP)
                status = u.get("status")
                is_off = status in ("offline", "error")
                card_bg = CARD_BG_OFF if is_off else CARD_BG
                draw.rounded_rectangle(
                    (x0, cy, x0 + COL_W, cy + CARD_H), radius=14, fill=card_bg,
                    outline=CARD_BORDER, width=1,
                )
                # 左侧平台色条（离线时弱化）
                bar_color = meta["color"] if not is_off else (60, 70, 85)
                draw.rounded_rectangle((x0 + 6, cy + 16, x0 + 10, cy + CARD_H - 16), radius=3, fill=bar_color)
                # 头像：复用 steam list 的 fetch_avatar（同一缓存 avatars/{sid}.jpg）。
                # 关键：保留 RGBA（Alpha 通道）——PNG 透明区域透出卡片底色，
                # 与 /steam list 显示效果完全一致；convert("RGB") 会白底毁掉观感。
                avatar_img = None
                if u.get("avatar_url"):
                    try:
                        from .steam_list import fetch_avatar
                        img = await fetch_avatar(
                            u.get("avatar_url"), data_dir or os.path.join(os.getcwd(), "data"),
                            str(u.get("sid", "")), proxy=proxy,
                        )
                        avatar_img = img  # 保留 RGBA
                    except Exception:
                        avatar_img = None
                if avatar_img is None:
                    avatar_img = Image.new("RGBA", (64, 64), (70, 80, 95, 255))
                avatar = _round_avatar(avatar_img, 44)
                if avatar.mode == "RGBA":
                    canvas.paste(avatar, (x0 + 26, cy + 34), avatar)
                else:
                    canvas.paste(avatar, (x0 + 26, cy + 34))
                # 名字
                name = _display_name(u)
                name_text = _truncate(draw, name, font_name, COL_W - 96 - 100)
                draw.text((x0 + 96, cy + 36), name_text, font=font_name,
                          fill=NAME_TEXT_OFF if is_off else NAME_TEXT)
                # 状态/游戏行（纯文本，避免 CJK 字体缺字形）
                if status == "playing" and u.get("game"):
                    gname = _truncate(draw, str(u["game"]), font_game, COL_W - 96 - 100)
                    draw.text((x0 + 100, cy + 58), gname, font=font_game, fill=GAME_TEXT)
                    if u.get("play_str"):
                        draw.text((x0 + 100, cy + 88), "时长：" + str(u["play_str"]), font=font_meta, fill=META_TEXT)
                elif is_off:
                    draw.text((x0 + 100, cy + 58), "离线", font=font_game, fill=GAME_TEXT_OFF)
                    if u.get("play_str"):
                        draw.text((x0 + 100, cy + 88), str(u["play_str"]), font=font_meta, fill=META_TEXT)
                else:
                    st = STATUS_TEXT.get(status, "在线")
                    draw.text((x0 + 100, cy + 58), st, font=font_game, fill=GAME_TEXT if not is_off else GAME_TEXT_OFF)
                    if u.get("poll_str"):
                        draw.text((x0 + 100, cy + 88), str(u["poll_str"]), font=font_meta, fill=META_TEXT)
                # 封面（右侧；离线无封面显示占位块）
                cover = None
                if not is_off and u.get("gameid") and cover_resolver is not None:
                    try:
                        resolved = await cover_resolver(u)
                        if resolved and os.path.isfile(str(resolved)):
                            cover = Image.open(str(resolved)).convert("RGB")
                        elif resolved:
                            cover = await _fetch_image(str(resolved), session)
                    except Exception:
                        cover = None
                if cover is None and u.get("cover_url"):
                    cover = await _fetch_image(u.get("cover_url"), session)
                if cover is not None:
                    # 竖版封面框，接近 Steam list 卡片，避免横框裁竖图过度放大
                    cw, ch = 72, CARD_H - 24
                    cimg = _fit_cover(cover, cw, ch)
                    canvas.paste(cimg, (x0 + COL_W - cw - 14, cy + 12))
                elif not is_off:
                    # 仅游玩/在线且无封面时画占位块（离线卡片右侧留空）
                    gname = (u.get("game") or u.get("name") or "·").strip()
                    draw.rounded_rectangle(
                        (x0 + COL_W - 86, cy + 14, x0 + COL_W - 14, cy + CARD_H - 14),
                        radius=10, fill=(34, 44, 58),
                    )
                    draw.text(
                        (x0 + COL_W - 50, cy + CARD_H / 2),
                        _truncate(draw, gname[:6], font_game, 60),
                        font=font_game, fill=(125, 140, 160), anchor="mm",
                    )
    finally:
        # 池化客户端视图不在此关闭，避免掐断插件全局 keep-alive
        pass

    out_path = os.path.join(os.getcwd() or ".", "data", "multiplatform_temp")
    os.makedirs(out_path, exist_ok=True)
    out_file = os.path.join(out_path, f"platform_list_{int(time.time() * 1000)}.png")
    canvas.save(out_file)
    return out_file
