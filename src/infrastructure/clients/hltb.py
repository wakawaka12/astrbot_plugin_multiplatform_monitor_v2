# -*- coding: utf-8 -*-
"""HowLongToBeat：优先经 ITAD 详情页拿到官方 HLTB 链接，再解析时长。

模拟浏览器 + 本地缓存；匹配失败宁可不显示，也不瞎猜。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, Optional
from urllib.parse import quote

import httpx

from ...shared.logging import logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
_CACHE_TTL = 7 * 86400


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-z一-鿿]+", "", str(s or "").lower())


def _seconds_to_hours(sec: Any) -> Optional[float]:
    try:
        v = float(sec)
    except (TypeError, ValueError):
        return None
    return round(v / 3600.0, 1) if v > 0 else None


class HLTBClient:
    def __init__(self, data_dir: str = "", proxy: Optional[str] = None):
        self.data_dir = data_dir or ""
        self.proxy = proxy
        self._cache_path = os.path.join(data_dir, "hltb_cache.json") if data_dir else ""
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        try:
            if self._cache_path and os.path.exists(self._cache_path):
                with open(self._cache_path, encoding="utf-8") as f:
                    return json.load(f) or {}
        except Exception as e:
            logger.warning(f"[HLTB] 缓存读取失败: {e}")
        return {}

    def _save_cache(self) -> None:
        try:
            if self._cache_path:
                os.makedirs(os.path.dirname(self._cache_path) or ".", exist_ok=True)
                with open(self._cache_path, "w", encoding="utf-8") as f:
                    json.dump(self._cache, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[HLTB] 缓存写入失败: {e}")

    def _cache_get(self, key: str):
        item = self._cache.get(key)
        if not isinstance(item, dict):
            return None
        try:
            if time.time() - float(item.get("ts") or 0) > _CACHE_TTL:
                return None
        except Exception:
            return None
        return item.get("data")

    def _cache_put(self, key: str, data) -> None:
        self._cache[key] = {"ts": time.time(), "data": data}
        self._save_cache()

    def format_line(self, data: dict) -> str:
        if not data:
            return ""
        parts = []
        if data.get("main_story") is not None:
            parts.append(f"主线约 {data['main_story']:g}h")
        if data.get("main_sides") is not None:
            parts.append(f"主线+支线 {data['main_sides']:g}h")
        if data.get("completionist") is not None:
            parts.append(f"全收集 {data['completionist']:g}h")
        if not parts:
            return ""
        return "⏱ " + " / ".join(parts) + "（HowLongToBeat 玩家统计）"

    def _parse_hltb_game_page(self, html: str) -> dict:
        data: dict = {}
        for key, pat in (
            ("main_story", r"Main\s*Story</h4><h5>\s*([\d.]+)\s*Hours?"),
            ("main_sides", r"Main\s*\+\s*Sides?</h4><h5>\s*([\d.]+)\s*Hours?"),
            ("completionist", r"Completionist</h4><h5>\s*([\d.]+)\s*Hours?"),
        ):
            m = re.search(pat, html, flags=re.I)
            if m:
                try:
                    data[key] = float(m.group(1))
                except ValueError:
                    pass
        if len(data) < 2:
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, flags=re.S)
            if m:
                try:
                    blob = json.loads(m.group(1))

                    def walk(o, acc):
                        if isinstance(o, dict):
                            if "comp_main" in o:
                                acc.append(o)
                            for v in o.values():
                                walk(v, acc)
                        elif isinstance(o, list):
                            for v in o:
                                walk(v, acc)

                    acc: list = []
                    walk(blob, acc)
                    for node in acc[:3]:
                        data.setdefault("main_story", _seconds_to_hours(node.get("comp_main")))
                        data.setdefault("main_sides", _seconds_to_hours(node.get("comp_plus")))
                        data.setdefault(
                            "completionist",
                            _seconds_to_hours(node.get("comp_100") or node.get("comp_all")),
                        )
                        if any(v is not None for v in data.values()):
                            break
                except Exception:
                    pass
        return {k: v for k, v in data.items() if v is not None}

    async def _fetch_hltb_id_from_itad(self, client: httpx.AsyncClient, slug: str) -> Optional[str]:
        if not slug:
            return None
        url = f"https://isthereanydeal.com/game/{quote(slug)}/info/"
        try:
            r = await client.get(url, headers={**UA, "Referer": "https://isthereanydeal.com/"}, timeout=12)
            if r.status_code != 200:
                return None
            m = re.search(r"howlongtobeat\.com/game/(\d+)", r.text, flags=re.I)
            return m.group(1) if m else None
        except Exception as e:
            logger.debug(f"[HLTB] ITAD 页失败 slug={slug}: {e}")
            return None

    async def lookup_by_hltb_id(self, hltb_id: str) -> Optional[dict]:
        hltb_id = str(hltb_id or "").strip()
        if not hltb_id.isdigit():
            return None
        cache_key = f"id:{hltb_id}"
        cached = self._cache_get(cache_key)
        if cached is not None:
            return cached or None
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=12, follow_redirects=True) as client:
                r = await client.get(
                    f"https://www.howlongtobeat.com/game/{hltb_id}",
                    headers={**UA, "Referer": "https://www.howlongtobeat.com/"},
                )
                r.raise_for_status()
                data = self._parse_hltb_game_page(r.text)
                if data:
                    data["game_id"] = hltb_id
                    self._cache_put(cache_key, data)
                    logger.info(f"[HLTB] id={hltb_id} -> {data}")
                    return data
                self._cache_put(cache_key, None)
                return None
        except Exception as e:
            logger.warning(f"[HLTB] 详情页失败 id={hltb_id}: {type(e).__name__}: {e}")
            self._cache_put(cache_key, None)
            return None

    async def lookup(self, title: str, itad_slug: str = "", itad_game_id: str = "") -> Optional[dict]:
        """查时长。优先：ITAD slug → 页内 HLTB 链接 → HLTB 详情。

        匹配不到就返回 None，避免瞎猜错游戏。
        """
        title = str(title or "").strip()
        slug = str(itad_slug or "").strip()
        key = f"{_norm(title)}|{slug}"
        if key and len(key) > 3:
            cached = self._cache_get(key)
            if cached is not None:
                return cached or None
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=True) as client:
                hltb_id = None
                if slug:
                    hltb_id = await self._fetch_hltb_id_from_itad(client, slug)
                if not hltb_id and itad_game_id:
                    # ITAD game 页有时直接带 id；用 slug 兜底已够
                    pass
                if hltb_id:
                    await asyncio.sleep(0.3)
                    data = await self.lookup_by_hltb_id(hltb_id)
                    if data:
                        data = {**data, "source": "itad-hltb"}
                        self._cache_put(key, data)
                        return data
                # 无 slug 或 ITAD 无链：不做盲搜（HTML 搜索不可靠，易匹配错游戏）
                self._cache_put(key, None)
                return None
        except Exception as e:
            logger.warning(f"[HLTB] lookup 失败 {title!r}: {type(e).__name__}: {e}")
            self._cache_put(key, None)
            return None
