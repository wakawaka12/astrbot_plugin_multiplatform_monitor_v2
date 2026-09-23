# -*- coding: utf-8 -*-
"""Steam 公开愿望单：游客读 store SSR items，带年龄门 cookie。"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import httpx

from ...shared.logging import logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client

_UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
}
# 年龄门：让 SSR 尽量带出成人向条目（不保证与客户端 100% 一致）
_AGE_COOKIES = {
    "birthtime": "568022401",
    "lastagecheckage": "1-0-1988",
    "wants_mature_content": "1",
    "Steam_Language": "schinese",
}
_CACHE_DIR = "wishlist_cache"
_CACHE_TTL = 6 * 3600


def _parse_ssr_items(html: str, steamid: str) -> List[str]:
    """从愿望单 HTML 的 SSR.loaderData 解析 items 数组，返回 appid 列表。"""
    m = re.search(r"window\.SSR\.loaderData\s*=\s*", html or "")
    raw = html[m.end():] if m else (html or "")
    norm = raw.replace("\\", "")
    idx = norm.find(str(steamid))
    items_idx = norm.find('"items":[{"appid"', idx if idx >= 0 else 0)
    if items_idx < 0:
        items_idx = norm.find('"items":[{"appid"')
    if items_idx < 0:
        return []
    br = norm.find("[", items_idx)
    if br < 0:
        return []
    try:
        arr, _ = json.JSONDecoder().raw_decode(norm[br:])
    except Exception as e:
        logger.warning(f"[wishlist] SSR items 解析失败: {e}")
        return []
    out: List[str] = []
    seen = set()
    for o in arr or []:
        if not isinstance(o, dict):
            continue
        aid = str(o.get("appid") or "").strip()
        if aid.isdigit() and aid not in seen:
            seen.add(aid)
            out.append(aid)
    return out


async def fetch_public_wishlist_appids(
    steamid: str,
    data_dir: str = "",
    proxy: Optional[str] = None,
    use_age_cookies: bool = True,
) -> Dict[str, Any]:
    """读公开愿望单。返回 {appids, cached, ts, url, note} 失败 appids=[]。"""
    steamid = str(steamid or "").strip()
    if not (steamid.isdigit() and len(steamid) == 17):
        return {"appids": [], "note": "SteamID 无效"}

    cache_dir = os.path.join(data_dir or "", _CACHE_DIR)
    cache_path = os.path.join(cache_dir, f"{steamid}.json") if data_dir else ""
    if cache_path:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            if os.path.exists(cache_path):
                data = json.loads(open(cache_path, encoding="utf-8").read() or "{}")
                ts = float(data.get("ts") or 0)
                if ts and time.time() - ts < _CACHE_TTL and isinstance(data.get("appids"), list):
                    return {
                        "appids": data["appids"],
                        "cached": True,
                        "ts": ts,
                        "note": "缓存",
                    }
        except Exception:
            pass

    url = f"https://store.steampowered.com/wishlist/profiles/{steamid}/"
    cookies = _AGE_COOKIES if use_age_cookies else None
    try:
        async with shared_httpx_client(proxy=proxy, timeout=20, follow_redirects=True) as client:
            r = await client.get(url, headers=_UA)
            if r.status_code != 200:
                return {"appids": [], "note": f"HTTP {r.status_code}", "url": url}
            appids = _parse_ssr_items(r.text, steamid)
    except Exception as e:
        logger.warning(f"[wishlist] 拉取失败 {steamid}: {type(e).__name__}: {e}")
        return {"appids": [], "note": str(e)[:80], "url": url}

    result = {
        "appids": appids,
        "cached": False,
        "ts": time.time(),
        "url": url,
        "note": f"公开 SSR {len(appids)} 条",
    }
    if cache_path:
        try:
            with open(cache_path, "w", encoding="utf-8") as f:
                json.dump({"ts": result["ts"], "appids": appids}, f)
        except Exception:
            pass
    logger.info(f"[wishlist] {steamid} -> {len(appids)} items")
    return result


async def resolve_wishlist_names(appids: List[str], proxy: Optional[str] = None) -> Dict[str, str]:
    """GetItems 批量取中文名。"""
    names: Dict[str, str] = {}
    ids = [str(a) for a in (appids or []) if str(a).isdigit()]
    if not ids:
        return names
    try:
        async with shared_httpx_client(proxy=proxy, timeout=15, follow_redirects=False) as client:
            for i in range(0, len(ids), 20):
                chunk = ids[i : i + 20]
                payload = {
                    "ids": [{"appid": a} for a in chunk],
                    "context": {"language": "schinese", "country_code": "CN"},
                    "data_request": {"include_basic_info": True},
                }
                params = {"input_json": json.dumps(payload, separators=(",", ":"))}
                try:
                    r = await client.get(
                        "https://api.steampowered.com/IStoreBrowseService/GetItems/v1/",
                        params=params,
                    )
                    for item in (r.json() or {}).get("response", {}).get("store_items") or []:
                        aid = str(item.get("id") or item.get("appid") or "")
                        if aid:
                            names[aid] = str(item.get("name") or "")
                except Exception as e:
                    logger.warning(f"[wishlist] GetItems 失败: {e}")
    except Exception as e:
        logger.warning(f"[wishlist] resolve names: {e}")
    return names
