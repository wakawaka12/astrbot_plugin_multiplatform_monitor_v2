# -*- coding: utf-8 -*-
"""开黑雷达：多人共有游戏 + 联机标签过滤，深色风格与 game_lib/rank 同源。"""
from __future__ import annotations

import asyncio
import io
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import httpx
from PIL import Image, ImageDraw

from ...shared.fonts import load_truetype
from ...shared.logging import format_exception, logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client
from .game_lib import (
    BG_BOTTOM,
    BG_TOP,
    CARD_BG,
    ICON_SIZE,
    MARGIN,
    WIDTH,
    _build_icon_candidates,
    _fetch_icon,
    _fmt_hours,
    _font,
    _truncate,
)

# Steam 联机相关 categories id
COOP_CATEGORY_IDS = {1, 9, 24, 27, 37, 38}
ROW_H = 56
MAX_ROWS = 12


def _extract_qq_list(text: str) -> List[str]:
    """从原始消息文本里抽出被 @ 的 QQ 号（去重保序）。

    兼容 CQ 码、At 文本，以及「@很长的昵称(123456)」——昵称不限 20 字。
    """
    import re

    raw = str(text or "")
    found: List[str] = []
    seen: Set[str] = set()

    def _add(qq: Optional[str]):
        if qq and qq not in seen:
            seen.add(qq)
            found.append(qq)

    # CQ / At 文本形态：可多次
    for m in re.finditer(r"\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]", raw):
        _add(m.group(1) or m.group(2))
    # @昵称(QQ) / @昵称（QQ）——昵称可能含空格、斜杠、逗号，用非贪婪扫到括号
    for m in re.finditer(r"@(.+?)\((\d{5,15})\)|@(.+?)（(\d{5,15})）", raw):
        _add(m.group(2) or m.group(4))
    # @纯数字
    for m in re.finditer(r"@(\d{5,15})", raw):
        _add(m.group(1))
    return found


def _extract_at_from_event(event) -> List[str]:
    """从消息链里的 At 组件取 QQ（比解析文本更稳）。"""
    found: List[str] = []
    seen: Set[str] = set()

    def _add(qq):
        qq = str(qq).strip() if qq is not None else ""
        if qq.isdigit() and qq not in seen:
            seen.add(qq)
            found.append(qq)

    segments = []
    try:
        getter = getattr(event, "get_messages", None)
        if callable(getter):
            segments = getter() or []
    except Exception:
        segments = []
    if not segments:
        try:
            msg_obj = getattr(event, "message_obj", None)
            segments = getattr(msg_obj, "message", None) or getattr(msg_obj, "message_chain", None) or []
        except Exception:
            segments = []

    for seg in segments or []:
        try:
            seg_type = getattr(seg, "type", None)
            if isinstance(seg, dict):
                seg_type = seg.get("type") or seg.get("msg_type") or seg_type
            type_l = str(seg_type or "").lower()
            # 数据字段
            qq = getattr(seg, "qq", None)
            if qq is None and isinstance(seg, dict):
                qq = seg.get("qq")
                if qq is None:
                    data = seg.get("data") or {}
                    qq = data.get("qq") or data.get("user_id") or data.get("id")
            if qq is None:
                qq = getattr(seg, "data", None)
                if isinstance(qq, dict):
                    qq = qq.get("qq") or qq.get("user_id") or qq.get("id")
            # 类名兜底
            cls_name = type(seg).__name__.lower()
            if "at" in type_l or "at" in cls_name or "mention" in cls_name:
                _add(qq)
            elif qq is not None and str(qq).isdigit() and ("at" in type_l or "at" in cls_name):
                _add(qq)
        except Exception:
            continue
    return found


def _resolve_qq_by_display_name(plugin, name: str, group_id: str = "") -> Optional[str]:
    """纯文本 @昵称（无 QQ 号）时，用绑定备注 / Steam 昵称反查 QQ。"""
    name = str(name or "").strip()
    if not name or len(name) < 2:
        return None
    bind_data = getattr(plugin, "_bind_data", {}) or {}
    for qq, info in bind_data.items():
        if str(qq).startswith("__remark:"):
            continue
        if not isinstance(info, dict):
            continue
        nick = str(info.get("nickname") or "").strip()
        if nick and nick != "*" and nick == name:
            return str(qq)
    # group_last_states 里的 Steam 昵称 -> 反查 sid -> 反查 qq
    for sid_states in (getattr(plugin, "group_last_states", {}) or {}).values():
        for sid, st in (sid_states or {}).items():
            if str(st.get("name") or "").strip() == name:
                qq = plugin._qq_of_bound_sid(sid) if hasattr(plugin, "_qq_of_bound_sid") else None
                if qq:
                    return str(qq)
    return None


def collect_coop_targets(plugin, event, target: str = "", group_id: str = "") -> List[str]:
    """汇总本次指令要对比的 SteamID 列表（@ 优先，否则本群）。"""
    raw_parts: List[str] = []
    for getter_name in ("get_message_str", "get_message"):
        getter = getattr(event, getter_name, None)
        if callable(getter):
            try:
                s = str(getter() or "")
                if s:
                    raw_parts.append(s)
                    break
            except Exception:
                pass
    if not raw_parts:
        raw_parts.append(str(getattr(event, "message_str", "") or ""))
    raw_parts.append(str(target or ""))

    qq_list: List[str] = []
    seen_qq: Set[str] = set()

    def _add_qq(qq):
        qq = str(qq).strip()
        if qq.isdigit() and qq not in seen_qq:
            seen_qq.add(qq)
            qq_list.append(qq)

    # 1) 消息链 At 组件（最准）
    for qq in _extract_at_from_event(event):
        _add_qq(qq)

    # 2) 文本解析
    for part in raw_parts:
        for qq in _extract_qq_list(part):
            _add_qq(qq)
        # 参数里的裸 QQ
        for tok in str(part or "").split():
            t = tok.strip()
            if t.isdigit() and 5 <= len(t) <= 12:
                _add_qq(t)

    # 3) 纯文本 @昵称 反查
    for part in raw_parts:
        import re as _re
        for m in _re.finditer(r"@([^@\s（(]{2,40})", str(part or "")):
            nick = m.group(1).strip()
            # 跳过已解析成数字的情况
            if nick.isdigit():
                continue
            qq = _resolve_qq_by_display_name(plugin, nick, group_id=group_id)
            if qq:
                _add_qq(qq)

    sids: List[str] = []
    unbound: List[str] = []
    for qq in qq_list:
        sid = None
        try:
            for s in plugin._bind_sids_for_qq(qq):
                s = str(s)
                if s.isdigit() and len(s) == 17:
                    sid = s
                    break
        except Exception:
            sid = None
        if sid:
            if sid not in sids:
                sids.append(sid)
        else:
            unbound.append(qq)
    return sids, unbound, qq_list


def _qq_to_steam_sid(plugin, qq: str) -> Optional[str]:
    for s in plugin._bind_sids_for_qq(str(qq)):
        s = str(s)
        if s.isdigit() and len(s) == 17:
            return s
    return None


async def _fetch_app_multiplayer_flags(
    appids: Sequence[int],
    proxy: Optional[str] = None,
    max_workers: int = 6,
    store_base: str = "https://store.steampowered.com",
) -> Dict[int, bool]:
    """批量查 appdetails，判断是否含联机/合作分类。查失败的按 True 保留（宁可多显示）。"""
    if not appids:
        return {}
    sem = asyncio.Semaphore(max_workers)
    result: Dict[int, bool] = {}

    async def _one(client: httpx.AsyncClient, appid: int):
        url = f"{store_base.rstrip('/')}/api/appdetails"
        async with sem:
            try:
                r = await client.get(url, params={"appids": str(appid), "filters": "categories"})
                if r.status_code != 200:
                    result[appid] = True
                    return
                data = r.json() or {}
                info = (data.get(str(appid)) or {}).get("data") or {}
                cats = info.get("categories") or []
                ids = {int(c.get("id")) for c in cats if c.get("id") is not None}
                if not ids:
                    result[appid] = True
                    return
                result[appid] = bool(ids & COOP_CATEGORY_IDS)
            except Exception as e:
                logger.debug(f"[coop] appdetails 失败 appid={appid}: {format_exception(e)}")
                result[appid] = True

    async with shared_httpx_client(proxy=proxy, timeout=12, follow_redirects=False) as client:
        await asyncio.gather(*(_one(client, int(a)) for a in appids))
    return result


def render_coop_card(
    players: List[Dict[str, Any]],
    games: List[Dict[str, Any]],
    font_path: Optional[str] = None,
    filtered: bool = True,
    note: str = "",
) -> Optional[bytes]:
    """players: [{sid, name}]；games: [{appid, name, owners, total_minutes, icon}] 已排序。"""
    n = min(len(games), MAX_ROWS)
    header_h = 100
    height = header_h + n * (ROW_H + 6) + MARGIN + 36
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
    font_sub = _font(font_path, 12)

    names = "、".join(p.get("name") or str(p.get("sid") or "")[:6] for p in players[:6])
    if len(players) > 6:
        names += f" 等{len(players)}人"
    title = "开黑雷达"
    bbox = draw.textbbox((0, 0), title, font=font_title)
    draw.text(((WIDTH - bbox[2] + bbox[0]) // 2, 12), title, font=font_title, fill=(255, 255, 255, 255))
    subtitle = f"{datetime.now().strftime('%Y/%m/%d')} · 共有 {len(games)} 款" + (
        " · 已筛联机" if filtered else ""
    )
    bbox2 = draw.textbbox((0, 0), subtitle, font=font_meta)
    draw.text(((WIDTH - bbox2[2] + bbox2[0]) // 2, 44), subtitle, font=font_meta, fill=(143, 152, 160, 255))
    who = _truncate(draw, names, font_sub, WIDTH - 40)
    bbox3 = draw.textbbox((0, 0), who, font=font_sub)
    draw.text(((WIDTH - bbox3[2] + bbox3[0]) // 2, 64), who, font=font_sub, fill=(109, 191, 246, 255))

    y = header_h
    for idx, game in enumerate(games[:MAX_ROWS]):
        x0, x1 = MARGIN, WIDTH - MARGIN
        draw.rounded_rectangle((x0, y, x1, y + ROW_H), radius=10, fill=CARD_BG + (255,))
        bar_color = (102, 192, 244, 255) if idx < 3 else (58, 90, 120, 255)
        draw.rounded_rectangle((x0 + 6, y + 10, x0 + 10, y + ROW_H - 10), radius=3, fill=bar_color)

        icon = game.get("icon")
        icon_x = x0 + 18
        icon_y = y + (ROW_H - ICON_SIZE) // 2
        if icon is not None:
            icon = icon.resize((ICON_SIZE, ICON_SIZE), Image.LANCZOS)
            mask = Image.new("L", (ICON_SIZE, ICON_SIZE), 0)
            ImageDraw.Draw(mask).rounded_rectangle((0, 0, ICON_SIZE, ICON_SIZE), radius=6, fill=255)
            img.paste(icon, (icon_x, icon_y), mask)
        else:
            draw.rounded_rectangle(
                (icon_x, icon_y, icon_x + ICON_SIZE, icon_y + ICON_SIZE),
                radius=6,
                fill=(60, 70, 85, 255),
            )

        name_x = icon_x + ICON_SIZE + 12
        right_x = x1 - 100
        name = _truncate(draw, game.get("name", "?"), font_name, right_x - name_x - 8)
        draw.text((name_x, y + 10), name, font=font_name, fill=(255, 255, 255, 255))
        owners = int(game.get("owners") or 0)
        total = game.get("total_minutes") or 0
        meta = f"{owners}人共有 · 合计 {_fmt_hours(total)}"
        draw.text((name_x, y + 32), meta, font=font_meta, fill=(180, 220, 255, 255))

        ow = str(game.get("owner_names") or "")
        if ow:
            ow_t = _truncate(draw, ow, font_meta, x1 - name_x - 12)
            ow_bbox = draw.textbbox((0, 0), ow_t, font=font_meta)
            draw.text((x1 - (ow_bbox[2] - ow_bbox[0]) - 14, y + 10), ow_t, font=font_meta, fill=(255, 200, 60, 255))
        y += ROW_H + 6

    if note:
        draw.text((MARGIN, height - 28), _truncate(draw, note, font_meta, WIDTH - 36), font=font_meta, fill=(140, 150, 160, 255))

    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG")
    return buf.getvalue()


async def find_common_games(
    plugin,
    sids: Sequence[str],
    proxy: Optional[str] = None,
    only_multiplayer: bool = True,
    cache_ttl: int = 6 * 3600,
) -> Dict[str, Any]:
    """拉取多人库并求交集。返回 {players, games, filtered, failed_sids}。"""
    import os
    import time
    import json

    data_dir = getattr(plugin, "data_dir", "") or ""
    cache_dir = os.path.join(data_dir, "owned_lib_cache") if data_dir else ""
    if cache_dir:
        os.makedirs(cache_dir, exist_ok=True)

    players: List[Dict[str, Any]] = []
    lib_map: Dict[str, Set[int]] = {}
    name_map: Dict[int, str] = {}
    play_map: Dict[int, int] = {}
    owner_names: Dict[int, List[str]] = {}
    failed: List[str] = []

    for sid in sids:
        sid = str(sid)
        if not (sid.isdigit() and len(sid) == 17):
            continue
        cache_path = os.path.join(cache_dir, f"{sid}.json") if cache_dir else ""
        games = None
        if cache_path and os.path.exists(cache_path):
            try:
                mtime = os.path.getmtime(cache_path)
                if time.time() - mtime < cache_ttl:
                    with open(cache_path, "r", encoding="utf-8") as f:
                        games = json.load(f)
            except Exception:
                games = None
        if games is None:
            games = await plugin.fetch_owned_games(sid)
            if games is None:
                failed.append(sid)
                continue
            if cache_path:
                try:
                    with open(cache_path, "w", encoding="utf-8") as f:
                        json.dump(games, f, ensure_ascii=False)
                except Exception:
                    pass
        pname = plugin._resolve_player_display_name(sid)
        players.append({"sid": sid, "name": pname})
        ids = set()
        for g in games or []:
            appid = int(g.get("appid") or 0)
            if not appid:
                continue
            ids.add(appid)
            name_map.setdefault(appid, str(g.get("name") or ""))
            play_map[appid] = play_map.get(appid, 0) + int(g.get("playtime_minutes") or 0)
            owner_names.setdefault(appid, []).append(pname)
        lib_map[sid] = ids

    if len(lib_map) < 2:
        return {"players": players, "games": [], "filtered": False, "failed_sids": failed, "need": 2}

    common: Optional[Set[int]] = None
    for ids in lib_map.values():
        common = ids if common is None else (common & ids)
    common = common or set()

    # 排序：共有人数多优先，其次合计时长
    scored = []
    for appid in common:
        owners = owner_names.get(appid, [])
        scored.append({
            "appid": appid,
            "name": name_map.get(appid, str(appid)),
            "owners": len(owners),
            "total_minutes": play_map.get(appid, 0),
            "owner_names": "、".join(owners[:4]),
        })
    scored.sort(key=lambda x: (-x["owners"], -x["total_minutes"], x["name"]))

    filtered = False
    if only_multiplayer and scored:
        check_ids = [g["appid"] for g in scored[:30]]
        flags = await _fetch_app_multiplayer_flags(check_ids, proxy=proxy)
        filtered_games = [g for g in scored if flags.get(g["appid"], True)]
        # 若筛完太少，回退展示未过滤列表，避免“没有共同游戏”的错觉
        if len(filtered_games) >= min(3, len(scored)):
            scored = filtered_games
            filtered = True
        else:
            scored = scored[:MAX_ROWS]
            filtered = False

    # 并发补图标（复用 game_lib 多 CDN + 缓存）
    top = scored[:MAX_ROWS]
    icons = await asyncio.gather(*(
        _fetch_icon({"appid": g["appid"], "name": g["name"]}, data_dir=data_dir, proxy=proxy)
        for g in top
    ))
    for g, icon in zip(top, icons):
        g["icon"] = icon
    scored = top

    return {"players": players, "games": scored, "filtered": filtered, "failed_sids": failed}
