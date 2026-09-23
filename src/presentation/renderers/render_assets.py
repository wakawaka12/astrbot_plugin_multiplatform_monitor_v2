# -*- coding: utf-8 -*-
"""渲染前资源并发预取：头像框 / 封面，避免串行 HTTP 拖慢出图。"""
from __future__ import annotations

import asyncio
from typing import Any, Dict, Iterable, List, Optional

from ...shared.logging import logger


async def gather_avatar_frames(data_dir: str, sids: Iterable[str], proxy=None, max_workers: int = 8) -> Dict[str, str]:
    """并发拉取 Steam 头像框路径；多平台 sid 直接跳过。"""
    from .game_start import get_avatar_frame_path, get_avatar_frame_url
    from ...infrastructure.clients.multi import split_platform_sid

    unique = []
    seen = set()
    for sid in sids:
        sid = str(sid or "")
        if not sid or sid in seen:
            continue
        if split_platform_sid(sid):
            continue  # PSN/Xbox 无 Steam 头像框
        seen.add(sid)
        unique.append(sid)
    if not unique:
        return {}

    sem = asyncio.Semaphore(max_workers)

    async def _one(sid: str):
        async with sem:
            try:
                fp = await get_avatar_frame_path(data_dir, sid, proxy=proxy)
                if fp:
                    return sid, fp
                url = await get_avatar_frame_url(sid, proxy=proxy)
                if url:
                    fp = await get_avatar_frame_path(data_dir, sid, url, proxy=proxy)
                    if fp:
                        return sid, fp
            except Exception as e:
                logger.debug(f"[预取] 头像框失败 {sid}: {e}")
        return sid, None

    results = await asyncio.gather(*(_one(s) for s in unique))
    return {sid: path for sid, path in results if path}


async def gather_steam_covers(
    plugin,
    user_list: List[Dict[str, Any]],
    proxy=None,
    max_workers: int = 6,
) -> Dict[str, str]:
    """并发拉取 Steam 游戏封面路径；多平台/无 gameid 跳过。"""
    from .game_start import get_cover_path
    from ...infrastructure.clients.multi import split_platform_sid

    tasks = []
    keys = []
    for u in user_list:
        sid = str(u.get("sid") or "")
        gid = u.get("gameid")
        if not sid or not gid or split_platform_sid(sid):
            continue
        keys.append(sid)
        tasks.append(
            get_cover_path(
                plugin.data_dir,
                gid,
                u.get("game") or "",
                sgdb_api_key=plugin.SGDB_API_KEY,
                appid=gid,
                proxy=proxy,
                sgdb_api_base=plugin.SGDB_API_BASE,
            )
        )
    if not tasks:
        return {}
    sem = asyncio.Semaphore(max_workers)

    async def _one(coro):
        async with sem:
            try:
                return await coro
            except Exception as e:
                logger.debug(f"[预取] 封面失败: {e}")
                return None

    paths = await asyncio.gather(*(_one(c) for c in tasks))
    return {sid: p for sid, p in zip(keys, paths) if p}
