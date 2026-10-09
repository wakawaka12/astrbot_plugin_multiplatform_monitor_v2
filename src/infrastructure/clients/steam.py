import asyncio
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Dict, Optional

import httpx

from ...shared.logging import format_exception, logger
from ...shared.network import (
    httpx_client_kwargs,
    rewrite_steam_cdn_url,
    shared_httpx_client,
    status_httpx_client,
    status_only_mode_enabled,
)
from .multi import split_platform_sid

# 商店页年龄门 cookie（与愿望单 SSR 一致，避免成人向游戏拿不到折扣倒计时）
_STORE_AGE_COOKIES = {
    "birthtime": "568022401",
    "lastagecheckage": "1-0-1988",
    "wants_mature_content": "1",
    "Steam_Language": "schinese",
}
_STORE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)

# 全局 Steam 商店限流（store.steampowered.com 非 WebAPI）
# 愿望单/查价/封面共用同一出口，任一路径 403 后其它路径也该降频
_STORE_BAN_UNTIL = 0.0
_STORE_403_STREAK = 0
_STORE_BAN_COOLDOWN_SEC = 900  # 15 分钟


def steam_store_ban_remaining() -> float:
    return max(0.0, _STORE_BAN_UNTIL - time.time())


def steam_store_403_streak() -> int:
    return int(_STORE_403_STREAK)


def steam_store_blocked() -> bool:
    return steam_store_ban_remaining() > 0


def note_steam_store_status(status_code: int | None, *, label: str = "") -> None:
    """根据商店接口状态码维护全局冷却。403 → 拉长 ban。"""
    global _STORE_BAN_UNTIL, _STORE_403_STREAK
    if status_code == 403:
        _STORE_403_STREAK += 1
        # 连续 403 时冷却更长，但设上限，避免扫描把 ban 推到天文数字
        extra = min(600, (_STORE_403_STREAK - 1) * 300)
        cool = min(_STORE_BAN_COOLDOWN_SEC + extra, 1800)
        _STORE_BAN_UNTIL = max(_STORE_BAN_UNTIL, time.time() + cool)
        logger.warning(
            f"[steam_store] 403 streak={_STORE_403_STREAK} ban+={cool}s "
            f"remain={steam_store_ban_remaining():.0f}s {label}"
        )
    elif status_code and 200 <= status_code < 300:
        _STORE_403_STREAK = 0


def steam_store_guard_msg() -> str:
    rem = steam_store_ban_remaining()
    if rem <= 0:
        return ""
    mins = int((rem + 59) // 60)
    return f"Steam 商店接口暂时被限流，请约 {mins} 分钟后再试（其它商店查询也在冷却中）。"


def parse_store_sale_end(html: str) -> dict:
    """从 Steam 商店页 HTML 提取折扣截止信息（尽力而为）。

    返回:
      {"end_ts": int|None, "end_text": str|None}
      end_text 示例：「特别促销！10 月 1 日截止」
    """
    text = html or ""
    end_ts = None
    end_text = None

    # 1) 倒计时 JS：DealTimer($DiscountCountdown, 1790010000) / OfferTimer(...)
    for pat in (
        r"DealTimer\s*\(\s*\$DiscountCountdown\s*,\s*(\d+)\s*\)",
        r"OfferTimer\s*\(\s*\$DiscountCountdown\s*,\s*(\d+)\s*\)",
        r"\$DiscountCountdown\s*,\s*(\d{9,})\s*\)",
        r"discount_countdown[^,]{0,40},\s*(\d{9,})\s*\)",
    ):
        m = re.search(pat, text, re.I)
        if m:
            try:
                ts = int(m.group(1))
                if 1_500_000_000 <= ts <= 2_200_000_000:
                    end_ts = ts
                    break
            except (TypeError, ValueError):
                pass

    # 2) 倒计时节点文案（中文/英文）
    m = re.search(
        r'game_purchase_discount_countdown"[^>]*>\s*([^<]{2,80})',
        text,
        re.I,
    )
    if m:
        end_text = re.sub(r"\s+", " ", m.group(1)).strip()

    # 3) 英文 Offer ends ...
    if not end_text:
        m = re.search(r"(Offer ends[^<\n]{0,60})", text, re.I)
        if m:
            end_text = m.group(1).strip()

    return {"end_ts": end_ts, "end_text": end_text}


def format_sale_end_line(end_ts=None, end_text=None, now: Optional[float] = None) -> str:
    """把截止信息格式化成推送文案；拿不到则返回空串。

    优先使用页面倒计时时间戳（更准）；否则退回截止文案。
    """
    if end_ts:
        try:
            now = float(now if now is not None else time.time())
            remain = int(end_ts) - int(now)
            if remain > 0:
                days = remain // 86400
                hours = (remain % 86400) // 3600
                minutes = (remain % 3600) // 60
                if days >= 1:
                    dur = f"{days} 天 {hours} 小时" if hours else f"{days} 天"
                elif hours >= 1:
                    dur = f"{hours} 小时 {minutes} 分" if minutes else f"{hours} 小时"
                else:
                    dur = f"{max(1, minutes)} 分钟"
                end_day = datetime.fromtimestamp(int(end_ts)).strftime("%m-%d %H:%M")
                return f"约 {dur}后结束（至 {end_day}）"
            return "折扣可能已结束"
        except (TypeError, ValueError, OSError):
            pass
    if end_text:
        t = end_text.strip()
        if any(k in t for k in ("截止", "剩余", "Offer ends", "offer ends", "Ends")):
            return t
        return f"商店：{t}"
    return ""


def steam_store_asset_cover_urls(appid: str, assets: dict) -> list:
    """GetItems assets → 可下载的封面 URL 列表（新版 Steam 哈希路径优先）。"""
    if not assets:
        return []
    aid = str(appid or "").strip()
    fmt = str(assets.get("asset_url_format") or (f"steam/apps/{aid}/${{FILENAME}}" if aid else ""))
    if not fmt:
        return []
    hosts = (
        "https://shared.akamai.steamstatic.com/store_item_assets/",
        "https://shared.cloudflare.steamstatic.com/store_item_assets/",
    )
    # 愿望单 40px 方图：小 capsule 更合适；header/main 作兜底
    keys = (
        "small_capsule",
        "small_capsule_2x",
        "header",
        "header_2x",
        "main_capsule",
        "main_capsule_2x",
        "library_600x900",
        "library_600x900_2x",
    )
    urls: list = []

    def add(url: str):
        if url and url not in urls:
            urls.append(url)

    for key in keys:
        fn = assets.get(key)
        if not fn:
            continue
        fn = str(fn)
        path = fmt.replace("${FILENAME}", fn)
        for host in hosts:
            add(host + path)
        # 无哈希目录的旧资源：旧 CDN 路径仍可能可用
        if "/" not in fn and aid:
            add(f"https://cdn.akamai.steamstatic.com/steam/apps/{aid}/{fn}")
            add(f"https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/{aid}/{fn}")
    return urls


def steam_cover_candidates(appid, header_image=None, capsule_image=None, assets=None) -> list:
    """封面 URL 候选：新版哈希 store_item_assets 优先，旧 CDN 作兜底。"""
    out: list = []

    def add(url):
        if url and url not in out:
            out.append(url)

    aid = str(appid or "").strip()
    if assets:
        for u in steam_store_asset_cover_urls(aid, assets):
            add(u)
    for url in (header_image, capsule_image):
        if not url:
            continue
        add(str(url))
        # 失效 CDN 主机名改写
        text = str(url)
        for dead in ("media.steamstatic.com", "steamcdn-a.akamaihd.net"):
            if dead in text:
                add(text.replace(dead, "cdn.akamai.steamstatic.com"))
    if aid:
        modern_hosts = (
            "https://shared.akamai.steamstatic.com/store_item_assets/steam/apps",
            "https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps",
        )
        modern_names = (
            "header.jpg",
            "header_schinese.jpg",
            "capsule_616x353.jpg",
            "capsule_231x87.jpg",
            "library_600x900.jpg",
        )
        for host in modern_hosts:
            for name in modern_names:
                add(f"{host}/{aid}/{name}")
        legacy_hosts = (
            "https://cdn.akamai.steamstatic.com/steam/apps",
            "https://cdn.cloudflare.steamstatic.com/steam/apps",
        )
        for host in legacy_hosts:
            for name in ("header.jpg", "capsule_231x87.jpg", "library_600x900.jpg"):
                add(f"{host}/{aid}/{name}")
    return out


class SteamClientError(RuntimeError):
    """Steam 客户端调用失败。"""


class SteamClientMixin:
    async def fetch_app_discounts(self, appids, country="CN", concurrency=5):
        """并发查询 appdetails 当前折扣，返回 {appid: {...}}。

        仅用于愿望单打折检测；失败的 appid 不会出现在结果里。
        """
        ids = []
        seen = set()
        for a in appids or []:
            s = str(a or "").strip()
            if s.isdigit() and s not in seen:
                seen.add(s)
                ids.append(s)
        if not ids:
            return {}
        # 全局商店冷却：禁止继续刷 appdetails（愿望单全量扫描曾把查价打挂）
        if steam_store_blocked():
            logger.warning(f"[wish_sale] 商店冷却中，跳过 appdetails 批量 n={len(ids)} remain={steam_store_ban_remaining():.0f}s")
            return {}
        store = (getattr(self, "STEAM_STORE_BASE", None) or "https://store.steampowered.com").rstrip("/")
        cc = str(country or "CN").lower()
        out = {}
        sem = asyncio.Semaphore(max(1, int(concurrency or 5)))

        async def _one(client, aid):
            if steam_store_blocked():
                return
            url = f"{store}/api/appdetails?appids={aid}&cc={cc}&l=schinese"
            try:
                async with sem:
                    if steam_store_blocked():
                        return
                    resp = await client.get(url)
                if resp.status_code != 200:
                    note_steam_store_status(resp.status_code, label=f"appdetails {aid}")
                    return
                note_steam_store_status(200)
                payload = (resp.json() or {}).get(aid) or {}
                if not payload.get("success"):
                    return
                data = payload.get("data") or {}
                po = data.get("price_overview") or {}
                final = po.get("final")
                initial = po.get("initial")
                cut = int(po.get("discount_percent") or 0)
                try:
                    cur = float(final) / 100.0 if final is not None else None
                except (TypeError, ValueError):
                    cur = None
                try:
                    reg = float(initial) / 100.0 if initial is not None else cur
                except (TypeError, ValueError):
                    reg = cur
                out[aid] = {
                    "appid": aid,
                    "name": str(data.get("name") or ""),
                    "cut": cut,
                    "current_price": cur,
                    "current_regular": reg if (reg is None or cur is None or reg >= cur) else cur,
                    "currency": str(po.get("currency") or "CNY"),
                    "is_free": bool(data.get("is_free")),
                    "header_image": str(data.get("header_image") or ""),
                }
            except Exception as e:
                logger.debug(f"[wish_sale] appdetails 失败 appid={aid}: {e}")

        try:
            # 代理出口可能被 Steam 商店 403；失败则直连再试
            proxies = []
            if getattr(self, "proxy", None):
                proxies.append(self.proxy)
            proxies.append(None)
            last_err = None
            out = {}
            for proxy in proxies:
                out = {}
                try:
                    async with shared_httpx_client(proxy=proxy, timeout=12, follow_redirects=False) as client:
                        await asyncio.gather(*(_one(client, aid) for aid in ids))
                except Exception as exc:
                    last_err = exc
                    logger.warning(f"[wish_sale] 批量折扣查询异常 proxy={proxy or 'direct'}: {format_exception(exc)}")
                    continue
                if out:
                    return out
            if last_err and not out:
                logger.warning(f"[wish_sale] 批量折扣查询无结果: {format_exception(last_err)}")
        except Exception as exc:
            logger.warning(f"[wish_sale] 批量折扣查询异常: {format_exception(exc)}")
        return out

    async def fetch_store_sale_end(self, appid, proxy=None) -> dict:
        """爬商店页提取折扣截止时间（倒计时时间戳 / 截止文案）。失败返回空 dict。

        仅建议在已确认有折扣时调用，降低风控概率。
        全局商店 403 冷却中时直接返回空，避免继续打商店。
        """
        if steam_store_blocked():
            return {}
        aid = str(appid or "").strip()
        if not aid.isdigit():
            return {}
        # 本地信息库优先
        try:
            from ...infrastructure.persistence import local_store as lstore

            conn = lstore.get_store(getattr(self, "data_dir", "") or "")
            cached = lstore.get_sale_end(conn, aid, max_age_sec=21600)
            if cached and (cached.get("end_ts") or cached.get("end_text")):
                return {"end_ts": cached.get("end_ts"), "end_text": cached.get("end_text"), "from_local": True}
        except Exception:
            pass
        store = (getattr(self, "STEAM_STORE_BASE", None) or "https://store.steampowered.com").rstrip("/")
        url = f"{store}/app/{aid}/?cc=cn&l=schinese"
        proxies = []
        if proxy is None:
            proxy = getattr(self, "proxy", None)
        if proxy:
            proxies.append(proxy)
        proxies.append(None)
        headers = {
            "User-Agent": _STORE_UA,
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        }
        for px in proxies:
            try:
                async with shared_httpx_client(
                    proxy=px, timeout=15.0, follow_redirects=True
                ) as client:
                    r = await client.get(url, headers=headers, cookies=_STORE_AGE_COOKIES)
                if r.status_code != 200:
                    note_steam_store_status(r.status_code, label=f"sale_end {aid}")
                    continue
                note_steam_store_status(r.status_code, label=f"sale_end {aid}")
                parsed = parse_store_sale_end(r.text)
                if parsed.get("end_ts") or parsed.get("end_text"):
                    try:
                        from ...infrastructure.persistence import local_store as lstore

                        conn = lstore.get_store(getattr(self, "data_dir", "") or "")
                        lstore.upsert_sale_end(
                            conn, aid, parsed.get("end_ts"), parsed.get("end_text")
                        )
                        conn.commit()
                    except Exception:
                        pass
                    return parsed
            except Exception as e:
                logger.debug(f"[sale_end] appid={aid} proxy={px or 'direct'}: {e}")
                continue
        return {}

    async def resolve_steam_cover_url(self, appid, proxy=None):
        """解析可用封面 URL：appdetails/GetItems 哈希路径优先，失败再试候选列表。

        返回可下载的图片 URL，或 None。
        """
        aid = str(appid or "").strip()
        if not aid.isdigit():
            return None
        proxy = proxy if proxy is not None else getattr(self, "proxy", None)
        header_image = ""
        capsule_image = ""
        assets = None
        store = (getattr(self, "STEAM_STORE_BASE", None) or "https://store.steampowered.com").rstrip("/")
        proxies = [proxy, None] if proxy else [None]
        # 1) appdetails
        for px in proxies:
            try:
                async with shared_httpx_client(proxy=px, timeout=10, follow_redirects=False) as client:
                    r = await client.get(f"{store}/api/appdetails", params={"appids": aid, "l": "schinese"})
                if r.status_code == 200:
                    payload = (r.json() or {}).get(aid) or {}
                    data = payload.get("data") if payload.get("success") else None
                    if data:
                        header_image = str(data.get("header_image") or "")
                        capsule_image = str(data.get("capsule_image") or data.get("capsule_imagev5") or "")
                        break
            except Exception as e:
                logger.debug(f"[cover] appdetails 失败 {aid} proxy={px or 'direct'}: {e}")
        # 2) GetItems assets
        for px in proxies:
            try:
                input_json = {
                    "ids": [{"appid": aid}],
                    "context": {"language": "schinese", "country_code": "CN"},
                    "data_request": {"include_basic_info": True, "include_assets": True},
                }
                params = {"input_json": json.dumps(input_json, ensure_ascii=False, separators=(",", ":"))}
                api_key = getattr(self, "API_KEY", "") or ""
                if api_key:
                    params["key"] = api_key
                async with shared_httpx_client(proxy=px, timeout=10, follow_redirects=False) as client:
                    r = await client.get(
                        f"{(getattr(self,'STEAM_API_BASE',None) or 'https://api.steampowered.com').rstrip('/')}/IStoreBrowseService/GetItems/v1/",
                        params=params,
                    )
                if r.status_code == 200:
                    items = (r.json() or {}).get("response", {}).get("store_items") or []
                    if items:
                        assets = items[0].get("assets") or None
                        if assets:
                            break
            except Exception as e:
                logger.debug(f"[cover] GetItems 失败 {aid} proxy={px or 'direct'}: {e}")
        candidates = steam_cover_candidates(
            aid, header_image=header_image, capsule_image=capsule_image, assets=assets
        )
        for px in proxies:
            try:
                async with shared_httpx_client(proxy=px, timeout=10, follow_redirects=False) as client:
                    for url in candidates:
                        try:
                            rr = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                        except Exception:
                            continue
                        if rr.status_code == 200 and rr.content and len(rr.content) > 500:
                            ctype = (rr.headers.get("content-type") or "").lower()
                            if ctype and "image" not in ctype and "octet-stream" not in ctype:
                                continue
                            return url
            except Exception as e:
                logger.debug(f"[cover] 候选下载失败 {aid} proxy={px or 'direct'}: {e}")
        return None

    async def fetch_owned_games(self, steam_id):
        """获取 Steam 玩家已购游戏列表（IPlayerService/GetOwnedGames）。

        返回: [{"appid": int, "name": str, "playtime_minutes": int,
                "playtime_2weeks": int, "last_played": int, "icon_url": str}]
        或 None（失败）/ []（隐私或无游戏）
        """
        if not self.API_KEY:
            raise SteamClientError("未配置 Steam API Key")
        url = (
            f"{self.STEAM_API_BASE}/IPlayerService/GetOwnedGames/v0001/"
            f"?key={self.API_KEY}&steamid={steam_id}&include_appinfo=1&format=json"
        )
        try:
            # GetOwnedGames 响应可达 MB 级（数千款游戏），经代理 20s 易 PoolTimeout -> 放宽到 60s
            async with shared_httpx_client(proxy=self.proxy, timeout=60, follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json().get("response", {})
                games = data.get("games", [])
                if not games:
                    return []
                result = []
                for g in games:
                    appid = g.get("appid")
                    if not appid:
                        continue
                    icon_hash = g.get("img_icon_url") or ""
                    # media.steamstatic.com 在部分网络下 TLS 失败；优先 akamai CDN
                    icon_url = (
                        f"https://cdn.akamai.steamstatic.com/steamcommunity/public/images/apps/{appid}/{icon_hash}.jpg"
                        if icon_hash else ""
                    )
                    result.append({
                        "appid": int(appid),
                        "name": str(g.get("name") or ""),
                        "playtime_minutes": int(g.get("playtime_forever") or 0),
                        "playtime_2weeks": int(g.get("playtime_2weeks") or 0),
                        "last_played": int(g.get("rtime_last_played") or 0),
                        "icon_url": icon_url,
                        "icon_hash": icon_hash,
                    })
                return result
        except Exception as exc:
            logger.warning(f"获取已购游戏失败: {format_exception(exc)} (SteamID: {steam_id})")
            return None

    async def fetch_community_perfect_count(self, steam_id) -> Optional[int]:
        """读取社区主页展示的 Perfect Games 数量（失败返回 None）。"""
        sid = str(steam_id or "").strip()
        if not (sid.isdigit() and len(sid) == 17):
            return None
        url = f"https://steamcommunity.com/profiles/{sid}/"
        proxies = []
        if getattr(self, "proxy", None):
            proxies.append(self.proxy)
        proxies.append(None)
        for px in proxies:
            try:
                async with shared_httpx_client(proxy=px, timeout=15, follow_redirects=True) as client:
                    r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    continue
                m = re.search(
                    r'data-tooltip-text="Games where this player has gotten every achievement\."[^>]*>\s*'
                    r'<div class="value">(\d+)</div>\s*<div class="label">Perfect Games</div>',
                    r.text or "",
                    re.I | re.S,
                )
                if not m:
                    m = re.search(
                        r'class="value">(\d+)</div>\s*<div class="label">Perfect Games</div>',
                        r.text or "",
                        re.I,
                    )
                if m:
                    return int(m.group(1))
            except Exception as e:
                logger.debug(f"[perfect] 社区页失败 proxy={px or 'direct'}: {e}")
        return None

    async def fetch_perfect_games(
        self,
        steam_id,
        api_key: str = "",
        max_check: int = 80,
        concurrency: int = 10,
        owned_games=None,
        cancel_event=None,
        progress_cb=None,
        progress_step: float = 0.1,
        only_appids=None,
    ):
        """查询玩家已达成「全部成就」的游戏列表。

        GetOwnedGames → 按时长取前 max_check（<=0 为全库）→ GetPlayerAchievements；
        全部 achievement.achieved=1 且条目>0 视为全成就。
        concurrency：成就 API 并发数，默认 10。
        owned_games：可传入已获取的游戏库，避免重复请求。
        cancel_event：asyncio.Event，set 后尽快停止扫描。
        progress_cb：可选回调 (attempted, total, perfect_n, pct)；约每 progress_step 比例触发一次。
        only_appids：仅扫描这些 appid（库存增量/联动时用）。
        """
        sid = str(steam_id or "").strip()
        if not (sid.isdigit() and len(sid) == 17):
            return None
        api_key = api_key or getattr(self, "API_KEY", "") or ""
        if not api_key:
            raise RuntimeError("未配置 Steam API Key")
        if owned_games is not None:
            games = owned_games
        else:
            games = await self.fetch_owned_games(sid)
        if games is None:
            return None
        owned = list(games)
        played = sorted(
            [g for g in owned if int(g.get("playtime_minutes") or 0) > 0],
            key=lambda x: int(x.get("playtime_minutes") or 0),
            reverse=True,
        )
        rest = [g for g in owned if int(g.get("playtime_minutes") or 0) <= 0]
        # 增量：只扫指定 appid（购游戏联动）
        if only_appids:
            only = {str(a) for a in only_appids if a}
            candidates = [g for g in (played + rest) if str(g.get("appid")) in only]
        elif max_check is None or int(max_check) <= 0 or int(max_check) >= len(owned):
            candidates = played + rest
        else:
            candidates = (played + rest)[: max(1, int(max_check))]
        api_base = (getattr(self, "STEAM_API_BASE", None) or "https://api.steampowered.com").rstrip("/")
        perfect = []
        try:
            conc = int(concurrency or 0)
        except (TypeError, ValueError):
            conc = 0
        if conc <= 0:
            conc = 10
        conc = max(2, min(24, conc))
        sem = asyncio.Semaphore(conc)
        checked = 0
        attempted = 0
        cancelled = False
        total = max(1, len(candidates))
        try:
            step = max(5, int(float(progress_step or 0.1) * 100))
        except (TypeError, ValueError):
            step = 10
        last_pct = {"v": 0}
        prog_lock = asyncio.Lock()

        def _is_cancelled() -> bool:
            nonlocal cancelled
            if cancel_event is not None and cancel_event.is_set():
                cancelled = True
                return True
            return False

        async def _emit_progress():
            if not progress_cb:
                return
            pct = min(100, int(attempted * 100 / total))
            if pct < last_pct["v"] + step and attempted < total:
                return
            last_pct["v"] = (pct // step) * step
            try:
                r = progress_cb(attempted, total, len(perfect), pct)
                if asyncio.iscoroutine(r):
                    await r
            except Exception as e:
                logger.debug(f"[perfect] progress_cb 失败: {e}")

        async def _one(client, g):
            nonlocal checked, attempted
            if _is_cancelled():
                return
            appid = str(g.get("appid") or "")
            if not appid.isdigit():
                return
            url = f"{api_base}/ISteamUserStats/GetPlayerAchievements/v1/"
            params = {"key": api_key, "steamid": sid, "appid": appid}
            try:
                async with sem:
                    if _is_cancelled():
                        return
                    resp = await client.get(url, params=params)
                attempted += 1
                if resp.status_code != 200:
                    return
                payload = resp.json() or {}
                stats = payload.get("playerstats") or {}
                if stats.get("success") is False:
                    return
                achs = stats.get("achievements") or []
                checked += 1
                if not achs:
                    return
                if all(int(a.get("achieved") or 0) == 1 for a in achs):
                    perfect.append({
                        "appid": appid,
                        "name": g.get("name") or stats.get("gameName") or appid,
                        "playtime_minutes": int(g.get("playtime_minutes") or 0),
                        "achievement_count": len(achs),
                        "icon_url": g.get("icon_url") or "",
                        "icon_hash": g.get("icon_hash") or "",
                    })
            except Exception as e:
                attempted += 1
                logger.debug(f"[perfect] {appid} 失败: {e}")
            # 进度：不阻塞扫描主路径，串行发提示时用锁
            if progress_cb and not _is_cancelled():
                try:
                    async with prog_lock:
                        await _emit_progress()
                except Exception:
                    pass

        proxies = []
        if getattr(self, "proxy", None):
            proxies.append(self.proxy)
        proxies.append(None)
        for px in proxies:
            if _is_cancelled():
                break
            try:
                async with shared_httpx_client(proxy=px, timeout=12, follow_redirects=False) as client:
                    await asyncio.gather(*(_one(client, g) for g in candidates))
                if checked > 0 or perfect or _is_cancelled():
                    break
            except Exception as e:
                logger.warning(f"[perfect] 批量查询异常 proxy={px or 'direct'}: {e}")

        if _is_cancelled():
            cancelled = True
        perfect.sort(key=lambda x: int(x.get("playtime_minutes") or 0), reverse=True)
        hint = None
        if not cancelled:
            try:
                hint = await self.fetch_community_perfect_count(sid)
            except Exception:
                hint = None
        return {
            "steamid": sid,
            "owned_count": len(owned),
            "checked": checked,
            "attempted": attempted,
            "games": perfect,
            "perfect_count_hint": hint,
            "cancelled": cancelled,
        }

    async def fetch_public_wishlist(self, steam_id, language="schinese"):
        """游客读取公开愿望单（Steam SSR items），不需要登录。

        返回:
          - None：请求失败或页面不是公开愿望单
          - []：公开，但 items 为空
          - [{"appid","priority","date_added","name",cover_url...}...]

        注意：
        - 公开 SSR 往往少于客户端计数（成人向/下架等可能不下发）。
        - 部分机房代理出口访问 store 愿望单会 403，会自动改直连重试。
        """
        sid = str(steam_id or "").strip()
        if not (sid.isdigit() and len(sid) == 17):
            return None
        # 全局冷却：连续 403 后暂时不再打愿望单 SSR，避免加重风控
        try:
            ban_until = float(getattr(self, "_wish_store_ban_until", 0) or 0)
        except (TypeError, ValueError):
            ban_until = 0.0
        if ban_until and time.time() < ban_until:
            left = int(ban_until - time.time())
            logger.warning(f"[wish] 愿望单冷却中（剩 {left}s），跳过 sid={sid}")
            return None
        store = (getattr(self, "STEAM_STORE_BASE", None) or "https://store.steampowered.com").rstrip("/")
        url = f"{store}/wishlist/profiles/{sid}/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        }

        proxies = []
        proxy_label = getattr(self, "proxy", None)
        now = time.time()
        proxy_skip_until = float(getattr(self, "_wish_ssr_proxy_skip_until", 0) or 0)
        direct_skip_until = float(getattr(self, "_wish_ssr_direct_skip_until", 0) or 0)
        if proxy_label:
            if not (proxy_skip_until and now < proxy_skip_until):
                proxies.append(proxy_label)
        if not (direct_skip_until and now < direct_skip_until):
            proxies.append(None)
        if not proxies:
            # 两条通道都在惩罚期
            left = int(min(
                x for x in (proxy_skip_until, direct_skip_until) if x > now
            ) - now) if any(x > now for x in (proxy_skip_until, direct_skip_until)) else 0
            logger.warning(f"[wish] 愿望单代理/直连均在惩罚期，跳过 sid={sid} 约剩 {left}s")
            return None

        html = None
        used_proxy = None
        got_403 = False
        for proxy in proxies:
            label = proxy or "direct"
            if got_403:
                await asyncio.sleep(2.0)
            try:
                async with shared_httpx_client(proxy=proxy, timeout=20, follow_redirects=True) as client:
                    resp = await client.get(url, headers=headers)
                    if resp.status_code == 403:
                        got_403 = True
                        # 该通道单独惩罚，优先改走另一条
                        skip_sec = 1800
                        if proxy:
                            self._wish_ssr_proxy_skip_until = time.time() + skip_sec
                        else:
                            self._wish_ssr_direct_skip_until = time.time() + skip_sec
                        logger.warning(f"[wish] HTTP 403 via {label} steamid={sid}（该通道暂停 {skip_sec}s）")
                        continue
                    if resp.status_code != 200:
                        logger.warning(f"[wish] HTTP {resp.status_code} via {label} steamid={sid}")
                        continue
                    text = resp.text or ""
                    if "window.SSR.loaderData" not in text:
                        logger.warning(f"[wish] 响应无 SSR via {label} steamid={sid} len={len(text)}")
                        continue
                    html = text
                    used_proxy = proxy
                    break
            except Exception as exc:
                logger.warning(f"[wish] 愿望单请求失败 via {label}: {format_exception(exc)} steamid={sid}")

        def _mark_403_streak():
            streak = int(getattr(self, "_wish_store_403_streak", 0) or 0) + 1
            self._wish_store_403_streak = streak
            if streak >= 2:
                try:
                    cool_min = float((getattr(self, "config", None) or {}).get("wish_sale_ban_cooldown_min", 45) or 45)
                except (TypeError, ValueError):
                    cool_min = 45.0
                cool = max(10, int(cool_min * 60))
                self._wish_store_ban_until = time.time() + cool
                self._wish_store_403_streak = 0
                logger.warning(f"[wish] 连续 403，愿望单 SSR 冷却 {cool//60} 分钟（代理+直连均受限）")
            else:
                self._wish_store_403_streak = streak

        if not html:
            if got_403:
                _mark_403_streak()
            return None
        try:
            self._wish_store_403_streak = 0
            # 成功后解除对应通道惩罚
            if used_proxy:
                self._wish_ssr_proxy_skip_until = 0
            else:
                self._wish_ssr_direct_skip_until = 0
            low = html.lower()
            if "this profile is private" in low or ("登录" in html[:2000] and "愿望单" not in html[:4000]):
                if "的愿望单" not in html and "wishlist" not in low[:8000]:
                    return None
            m = re.search(r"window\.SSR\.loaderData\s*=\s*", html)
            if not m:
                return None
            raw = html[m.end(): m.end() + 800000]
            norm = raw.replace("\\", "")
            idx = norm.find(sid)
            items_idx = norm.find('"items":[{"appid"', idx if idx >= 0 else 0)
            if items_idx < 0:
                items_idx = norm.find('"items":[{')
            if items_idx < 0:
                return None
            br = norm.find("[", items_idx)
            arr, _ = json.JSONDecoder().raw_decode(norm[br:])
            items = []
            for o in arr if isinstance(arr, list) else []:
                if not isinstance(o, dict):
                    continue
                aid = o.get("appid")
                if aid:
                    items.append({
                        "appid": str(aid),
                        "priority": o.get("priority"),
                        "date_added": o.get("date_added"),
                        "name": "",
                    })
            if not items:
                return []
            # 批量中文名 + 封面（API 优先走代理，失败再直连；避免再打商店页）
            ids = [it["appid"] for it in items]
            names = {}
            covers = {}
            api_proxies = []
            if getattr(self, "proxy", None):
                api_proxies.append(self.proxy)
            api_proxies.append(None)
            for api_proxy in api_proxies:
                if names:
                    break
                try:
                    async with shared_httpx_client(proxy=api_proxy, timeout=15, follow_redirects=False) as client:
                        for j in range(0, len(ids), 20):
                            chunk = ids[j:j + 20]
                            input_json = {
                                "ids": [{"appid": a} for a in chunk],
                                "context": {"language": language, "country_code": "CN"},
                                "data_request": {
                                    "include_basic_info": True,
                                    "include_assets": True,
                                },
                            }
                            params = {"input_json": json.dumps(input_json, ensure_ascii=False, separators=(",", ":"))}
                            api_key = getattr(self, "API_KEY", "") or ""
                            if api_key:
                                params["key"] = api_key
                            try:
                                gi = await client.get(
                                    f"{(getattr(self,'STEAM_API_BASE',None) or 'https://api.steampowered.com').rstrip('/')}/IStoreBrowseService/GetItems/v1/",
                                    params=params,
                                )
                                if gi.status_code == 200:
                                    for entry in (gi.json() or {}).get("response", {}).get("store_items") or []:
                                        aid = str(entry.get("id") or entry.get("appid") or "")
                                        if aid:
                                            names[aid] = str(entry.get("name") or "")
                                            covers[aid] = steam_store_asset_cover_urls(aid, entry.get("assets") or {})
                            except Exception as e:
                                logger.debug(f"[wish] GetItems 失败: {e}")
                except Exception as e:
                    logger.debug(f"[wish] GetItems client 失败 proxy={api_proxy or 'direct'}: {e}")
            for it in items:
                it["name"] = names.get(it["appid"]) or ""
                cover_urls = covers.get(it["appid"]) or []
                it["cover_urls"] = cover_urls
                it["cover_url"] = cover_urls[0] if cover_urls else ""
            logger.info(f"[wish] 愿望单 OK sid={sid} n={len(items)} via={used_proxy or 'direct'}")
            return items
        except Exception as exc:
            logger.warning(f"[wish] 公开愿望单解析失败: {format_exception(exc)} steamid={sid}")
            return None

    def _status_proxy_candidates(self):
        """状态接口代理候选。

        实测（2026-09-21）：GetPlayerSummaries 带 39 个 steamids 时，
        直连 SSL/ReadTimeout 可达 8–46s；经 mihomo 代理约 0.6s 且 players 齐全。
        因此配置了代理时 **优先走代理**，失败再回落直连。
        """
        px = getattr(self, "proxy", None) or None
        if px:
            # 实测 2026-10-09：Steam 直连已被限流（首次成功后连续超时），
            # 代理是唯一可靠通道 → 代理优先并重试一次，最后才回落直连兜底
            return [px, px, None]
        return [None]

    async def _status_get_json(self, url, timeout=15):
        """按候选代理顺序请求 WebAPI，返回 (payload, used_proxy)。"""
        last_exc = None
        for px in self._status_proxy_candidates():
            t = timeout if px else min(10.0, timeout)
            try:
                async with status_httpx_client(proxy=px, timeout=t, follow_redirects=False) as client:
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        raise Exception(f"HTTP {resp.status_code}")
                    return resp.json(), px
            except Exception as e:
                last_exc = e
                logger.warning(f"[状态池] 请求失败 proxy={px or 'direct'} timeout={t}: {format_exception(e)}")
        raise last_exc or Exception("status request failed")

    async def fetch_player_summary(self, steam_id):
        """获取 Steam 玩家原始摘要，由应用层负责字段本地化与展示。"""
        if not self.API_KEY:
            raise SteamClientError("未配置 Steam API Key")
        url = (
            f"{self.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
            f"?key={self.API_KEY}&steamids={steam_id}"
        )
        try:
            data, _px = await self._status_get_json(url, timeout=15)
            players = (data.get("response") or {}).get("players") or []
            return players[0] if players else None
        except Exception as exc:
            logger.warning(f"获取 Steam 玩家摘要失败: {format_exception(exc)} (SteamID: {steam_id})")
            raise SteamClientError(f"Steam API 请求失败: {format_exception(exc)}") from exc

    async def fetch_player_status(self, steam_id, retry=None):
        '''拉取单个玩家的状态，失败自动重试多次并指数退避。
        支持平台前缀（psn:/xbox:/nso:），自动委托多平台客户端获取。'''
        sp = split_platform_sid(str(steam_id))
        if sp:
            platform, raw_id = sp
            statuses = await self.fetch_multi_statuses(platform, [raw_id])
            return statuses.get(raw_id)
        url = (
            f"{self.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
            f"?key={self.API_KEY}&steamids={steam_id}"
        )
        delay = 1
        retry = retry if retry is not None else self.RETRY_TIMES
        for attempt in range(retry):
            try:
                data, _px = await self._status_get_json(url, timeout=15)
                resp_data = data.get('response')
                if not isinstance(resp_data, dict):
                    raise Exception(f"Steam 返回异常响应（类型={type(resp_data).__name__}，值={resp_data}），疑似 API Key 无效或触发限流")
                if not resp_data.get('players'):
                    raise Exception("响应中无玩家数据")
                player = data['response'].get('players')[0]
                # 返回更多字段，包括头像
                return {
                    'name': player.get('personaname'),
                    'gameid': player.get('gameid'),
                    'lastlogoff': player.get('lastlogoff'),
                    'gameextrainfo': player.get('gameextrainfo'),
                    'personastate': player.get('personastate', 0),
                    'avatarfull': player.get('avatarfull'),
                    'avatar': player.get('avatar')
                }
            except Exception as e:
                logger.warning(f"拉取 Steam 状态失败: {format_exception(e)} (SteamID: {steam_id}, 第{attempt+1}次重试)")
                if attempt < retry - 1:
                    await asyncio.sleep(delay)
                    delay *= 2
        logger.error(f"SteamID {steam_id} 状态获取失败，已重试{retry}次")
        return None

    async def fetch_player_statuses_batch(self, steam_ids, retry=None):
        '''批量拉取多个玩家的状态（单次请求最多 100 个 ID）。
        返回 {player_key: status_dict}，缺失或失败的 key 不在返回字典中。
        - 纯 SteamID64：Steam GetPlayerSummaries/v2 批量接口（≤100/次）
        - 带平台前缀（psn:/xbox:/nso:）的玩家自动拆分，走多平台客户端
        相比逐个请求可大幅降低 API 调用次数，避免触发 Steam 限流（429 / x-eresult:84）。
        '''
        if not steam_ids or not (self.API_KEY or any(split_platform_sid(str(s)) for s in steam_ids)):
            return {}
        # 拆分 Steam 与多平台玩家
        plain_ids: list = []
        multi_map: Dict[str, list] = {}
        for sid in steam_ids:
            sp = split_platform_sid(str(sid))
            if sp:
                multi_map.setdefault(sp[0], []).append(sp[1])
            else:
                plain_ids.append(sid)
        result: dict = {}
        if plain_ids and self.API_KEY:
            result.update(await self._fetch_steam_batch(plain_ids, retry))
        for platform, ids in multi_map.items():
            try:
                partial = await self.fetch_multi_statuses(platform, ids)
                for raw, st in partial.items():
                    result[f"{platform}:{raw}"] = st
            except Exception as e:
                logger.warning(f"[批量查询] {platform} 拉取失败: {format_exception(e)}")
        return result

    async def _fetch_steam_batch(self, steam_ids, retry=None):
        """Steam 批量查询（仅纯 SteamID64），批量失败自动降级为单查。"""
        result = {}
        retry = 1 if retry is None else max(1, int(retry))
        # 状态推送优先：批量只试 1～2 次，避免重试风暴占满连接池
        retry = min(retry, 2)
        BATCH_SIZE = 100
        id_batches = [steam_ids[i:i+BATCH_SIZE] for i in range(0, len(steam_ids), BATCH_SIZE)]
        for batch in id_batches:
            ids_str = ",".join(batch)
            url = (
                f"{self.STEAM_API_BASE}/ISteamUser/GetPlayerSummaries/v2/"
                f"?key={self.API_KEY}&steamids={ids_str}"
            )
            delay = 1
            for attempt in range(retry):
                try:
                    data, used_px = await self._status_get_json(url, timeout=15)
                    resp_data = data.get('response')
                    if not isinstance(resp_data, dict):
                        logger.warning(f"[批量查询] Steam 返回异常响应（类型={type(resp_data).__name__}，值={resp_data}），疑似 API Key 无效或触发限流，本批降级处理")
                        resp_data = {}
                    players = resp_data.get('players') or []
                    for player in players:
                        sid = player.get('steamid')
                        if sid and sid in batch:
                            result[sid] = {
                                'name': player.get('personaname'),
                                'gameid': player.get('gameid'),
                                'lastlogoff': player.get('lastlogoff'),
                                'gameextrainfo': player.get('gameextrainfo'),
                                'personastate': player.get('personastate', 0),
                                'avatarfull': player.get('avatarfull'),
                                'avatar': player.get('avatar')
                            }
                    missing = [s for s in batch if s not in result]
                    if missing:
                        logger.warning(f"[批量查询] 以下 SteamID 在响应中缺失（可能无效/隐私）: {missing}")
                    logger.debug(f"[批量查询] OK n={len(players)} proxy={used_px or 'direct'}")
                    break
                except Exception as e:
                    logger.warning(f"[批量查询] 失败: {format_exception(e)} (本批 {len(batch)} 个 ID, 第{attempt+1}次重试)")
                    if attempt < retry - 1:
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"[批量查询] 本批彻底失败: batch_n={len(batch)}")
                        # 不再对每个 sid 做单查降级（会放大连接占用）；交给下一轮智能轮询
        return result

    async def resolve_steam_input(self, raw):
        '''将多种格式的 Steam 输入统一解析为 17 位 SteamID64。
        支持：
        - 17 位纯数字 SteamID64
        - https://steamcommunity.com/profiles/<steamid64>
        - https://steamcommunity.com/id/<vanity>  （自定义 ID，调 ResolveVanityURL）
        - https://s.team/p/<steamid64> 或 s.team/p/<steamid64>
        - 8 位好友码（SteamID32 + 76561197960265728 = SteamID64）
        返回 SteamID64 字符串；解析失败返回 None。
        '''
        if not raw or not isinstance(raw, str):
            return None
        s = raw.strip()
        # 1) 纯 17 位数字
        if s.isdigit() and len(s) == 17:
            return s
        # 2) URL：提取路径段
        lowered = s.lower()
        if 'steamcommunity.com' in lowered or 's.team/p/' in lowered:
            # 去掉 query 和 fragment
            path = s.split('?')[0].split('#')[0].rstrip('/')
            segments = path.split('/')
            # 例: https://steamcommunity.com/profiles/76561198xxx
            #     https://steamcommunity.com/id/customname
            #     https://s.team/p/76561198xxx
            if len(segments) >= 2:
                last = segments[-1]
                last2 = segments[-2] if len(segments) >= 2 else ''
                if last2 == 'profiles' and last.isdigit() and len(last) == 17:
                    return last
                if last2 == 'id' and last:
                    # 自定义 vanity URL，需调用 API 解析
                    return await self._resolve_vanity_url(last)
                # s.team/p/<id>
                if 's.team' in lowered and last.isdigit() and len(last) == 17:
                    return last
        # 3) 好友码（SteamID32）；拒绝前导 0 的手滑输入（如 067206521 少了 1）
        if s.isdigit() and len(s) <= 12:
            raw_digits = s.lstrip("0") or "0"
            if s != raw_digits and len(s) >= 2:
                # 前导 0：可能是少打了一位，不直接换算，避免绑错号
                logger.warning(
                    f"[steam_id] 输入 {s} 含前导 0，已忽略。若好友码为 {raw_digits} 请去掉 0 后重试；"
                    f"完整好友码示例：1067206521"
                )
                return None
            try:
                steamid64 = str(int(raw_digits) + 76561197960265728)
                if len(steamid64) == 17:
                    return steamid64
            except Exception:
                pass
        return None

    async def _resolve_vanity_url(self, vanity):
        '''调用 Steam ResolveVanityURL 接口把自定义 ID 转成 SteamID64'''
        if not self.API_KEY or not vanity:
            return None
        url = (
            f"{self.STEAM_API_BASE}/ISteamUser/ResolveVanityURL/v1/"
            f"?key={self.API_KEY}&vanityurl={vanity}"
        )
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=False) as client:
                resp = await client.get(url)
                if resp.status_code != 200:
                    logger.warning(f"[vanity解析] HTTP {resp.status_code} (vanity={vanity})")
                    return None
                data = resp.json()
                resp_data = data.get('response')
                if not isinstance(resp_data, dict):
                    resp_data = {}
                success = resp_data.get('success', 0)
                steamid = resp_data.get('steamid')
                if success == 1 and steamid:
                    return steamid
                logger.warning(f"[vanity解析] 失败 success={success} (vanity={vanity})")
                return None
        except Exception as e:
            logger.warning(f"[vanity解析] 异常: {e} (vanity={vanity})")
            return None

    async def _review_summary(self, appid, language="all"):
        """获取 Steam 商店评价摘要。language 传 'all' 表示所有语言（缺省），否则为指定语言。
        返回 {"text","percent","total"} 或 None。"""
        gid = str(appid).strip()
        if not gid.isdigit():
            return None
        url = f"{self.STEAM_STORE_BASE}/appreviews/{gid}"
        params = {"json": 1, "filter": "summary"}
        language = language or "all"
        params["language"] = language
        try:
            async with shared_httpx_client(proxy=None, timeout=15, follow_redirects=False) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                payload = response.json()
                summary = payload.get("query_summary") or payload.get("querySummary") or {}
                total = int(summary.get("total_reviews") or summary.get("totalReviews") or 0)
                positive = int(summary.get("total_positive") or summary.get("totalPositive") or 0)
                if total <= 0:
                    return {"text": "暂无评价", "percent": None, "total": 0}
                percent = round(positive * 100 / total)
                if percent >= 95:
                    label = "好评如潮"
                elif percent >= 80:
                    label = "特别好评"
                elif percent >= 70:
                    label = "多半好评"
                elif percent >= 40:
                    label = "褒贬不一"
                elif percent >= 20:
                    label = "多半差评"
                else:
                    label = "差评"
                return {"text": label, "percent": percent, "total": total}
        except Exception as exc:
            logger.warning(f"获取 Steam 评价摘要失败: {exc} (appid={gid})")
            return None

    async def fetch_game_reviews(self, appid, language="schinese"):
        """获取 Steam 商店评价摘要（默认简体中文；language=None 表示全部语言）。"""
        return await self._review_summary(appid, language)

    async def fetch_game_reviews_both(self, appid):
        """同时获取「全部语言」与「简体中文」两份评价摘要，供卡片并列显示。"""
        all_review, zh_review = await asyncio.gather(
            self._review_summary(appid, None),
            self._review_summary(appid, "schinese"),
        )
        return {"all": all_review, "schinese": zh_review}

    async def fetch_game_details(self, appid, language="schinese"):
        """获取 Steam 商店游戏详情。"""
        gid = str(appid).strip()
        if not gid.isdigit():
            return None
        if steam_store_blocked():
            return None
        url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l={language}&cc=cn"
        try:
            async with shared_httpx_client(proxy=None, timeout=15, follow_redirects=False) as client:
                response = await client.get(url)
                note_steam_store_status(response.status_code, label=f"details {gid}")
                response.raise_for_status()
                payload = response.json().get(gid, {})
                return payload.get("data") if payload.get("success") else None
        except Exception as exc:
            logger.warning(f"获取 Steam 游戏详情失败: {format_exception(exc)} (appid={gid})")
            return None

    async def fetch_app_meta(self, appid, country="CN"):
        """取 appdetails 的 type + 中文名。返回 {type, name} 或 None。"""
        gid = str(appid).strip()
        if not gid.isdigit():
            return None
        if steam_store_blocked():
            return None
        url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&cc={str(country).lower()}&l=schinese"
        try:
            async with shared_httpx_client(proxy=None, timeout=12, follow_redirects=False) as client:
                response = await client.get(url)
                note_steam_store_status(response.status_code, label=f"meta {gid}")
                response.raise_for_status()
                payload = response.json().get(gid, {})
                data = payload.get("data") if payload.get("success") else None
                if not data:
                    return None
                return {
                    "type": str(data.get("type") or "").lower(),
                    "name": str(data.get("name") or "").strip(),
                }
        except Exception as exc:
            logger.warning(f"获取 Steam app meta 失败: {exc} (appid={gid})")
            return None

    async def fetch_app_types_batch(self, appids, language="schinese"):
        """IStoreBrowseService/GetItems 批量取 type/名称（一次请求查多个 appid）。

        实测（2026-09）：多数环境 **无需 API Key** 也能访问；
        type 字段：0 = 游戏本体，4 = DLC/附属（item_type 不可区分）。

        返回 {appid_str: {"type": "game"|"dlc"|..., "name": str}}
        失败时返回 {}，由调用方回退标题启发。
        """
        ids = []
        seen = set()
        for a in appids or []:
            s = str(a or "").strip()
            if s.isdigit() and s not in seen:
                seen.add(s)
                ids.append(s)
        if not ids:
            return {}
        # GetItems 数值 type → 语义类型
        type_map = {
            0: "game",
            4: "dlc",
            1: "dlc",
            2: "music",
            3: "demo",
            5: "dlc",
            6: "video",
        }
        chunks = [ids[i:i + 20] for i in range(0, len(ids), 20)]
        out = {}
        base = (getattr(self, "STEAM_API_BASE", None) or "https://api.steampowered.com").rstrip("/")
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=False) as client:
                for chunk in chunks:
                    input_json = {
                        "ids": [{"appid": x} for x in chunk],
                        "context": {"language": language, "country_code": "CN"},
                        "data_request": {"include_basic_info": True},
                    }
                    params = {"input_json": json.dumps(input_json, ensure_ascii=False, separators=(",", ":"))}
                    api_key = getattr(self, "API_KEY", "") or ""
                    if api_key:
                        params["key"] = api_key
                    url = f"{base}/IStoreBrowseService/GetItems/v1/"
                    resp = await client.get(url, params=params)
                    if resp.status_code != 200:
                        logger.warning(f"[GetItems] HTTP {resp.status_code} n={len(chunk)}")
                        continue
                    items = (resp.json() or {}).get("response", {}).get("store_items") or []
                    for entry in items:
                        if not isinstance(entry, dict):
                            continue
                        aid = str(entry.get("appid") or entry.get("id") or "")
                        if not aid:
                            continue
                        raw_type = entry.get("type")
                        typ_s = ""
                        try:
                            if raw_type is not None:
                                if isinstance(raw_type, int):
                                    typ_s = type_map.get(raw_type, "")
                                else:
                                    ts = str(raw_type).strip().lower()
                                    typ_s = type_map.get(ts, ts)
                        except Exception:
                            typ_s = ""
                        name = str(entry.get("name") or "").strip()
                        bi = entry.get("basic_info") or {}
                        pubs = []
                        if isinstance(bi, dict):
                            for p in (bi.get("publishers") or [])[:3]:
                                if isinstance(p, dict) and p.get("name"):
                                    pubs.append(str(p.get("name")))
                        out[aid] = {
                            "type": typ_s,
                            "name": name,
                            "raw_type": raw_type,
                            "publishers": pubs,
                            "header": "",
                        }
                        assets = entry.get("assets") or {}
                        if isinstance(assets, dict):
                            # 部分字段是相对路径
                            h = assets.get("header") or assets.get("main_capsule") or ""
                            fmt = assets.get("asset_url_format") or ""
                            if h and str(h).startswith("http"):
                                out[aid]["header"] = str(h)
                            elif h and fmt:
                                # {path} 模板
                                out[aid]["header"] = str(fmt).replace("{filename}", str(h)).replace("{path}", str(h))
        except Exception as e:
            logger.warning(f"[GetItems] 批量 type 失败: {type(e).__name__}: {e}")
        return out


    async def fetch_region_price(self, appid, country="CN"):
        """获取指定国家区 Steam 商店价格（含币种、折后价/原价/折扣）。

        优先 appdetails；失败时尝试 packagedetails（package/sub id，如数字豪华版）。
        """
        gid = str(appid).strip()
        if not gid.isdigit():
            return None
        if steam_store_blocked():
            logger.debug(f"[price] 商店冷却中，跳过 region_price {gid}/{country}")
            return None
        url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&cc={str(country).lower()}"
        try:
            async with shared_httpx_client(proxy=None, timeout=15, follow_redirects=False) as client:
                response = await client.get(url)
                note_steam_store_status(response.status_code, label=f"region_price {gid}/{country}")
                response.raise_for_status()
                payload = response.json().get(gid, {})
                data = payload.get("data") if payload.get("success") else None
                if not data:
                    return await self.fetch_package_price(gid, country)
                price_overview = data.get("price_overview") or {}
                if not price_overview:
                    return await self.fetch_package_price(gid, country)
                return {
                    "currency": price_overview.get("currency"),
                    "current_price": price_overview.get("final", 0) / 100,
                    "current_regular": price_overview.get("initial", 0) / 100,
                    "cut": price_overview.get("discount_percent", 0),
                }
        except Exception as exc:
            logger.warning(f"获取 Steam {country} 区价格失败: {exc} (appid={gid})")
            try:
                return await self.fetch_package_price(gid, country)
            except Exception:
                return None

    async def fetch_package_details(self, packageid, country="CN"):
        """Steam packagedetails：package/sub（数字豪华版等捆绑包）详情。

        返回 {name, price:{...}, header_image, apps:[{id,name}], currency...} 或 None
        """
        pid = str(packageid).strip()
        if not pid.isdigit():
            return None
        url = f"{self.STEAM_STORE_BASE}/api/packagedetails?packageids={pid}&cc={str(country).lower()}"
        try:
            async with shared_httpx_client(proxy=None, timeout=12, follow_redirects=False) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json().get(pid, {})
                if not payload.get("success"):
                    return None
                data = payload.get("data") or {}
                price = data.get("price") or data.get("price_overview") or {}
                return {
                    "name": str(data.get("name") or "").strip(),
                    "price": price,
                    "header_image": data.get("header_image") or data.get("page_image") or data.get("small_logo") or "",
                    "apps": data.get("apps") or [],
                    "packageid": pid,
                    "country": str(country).upper(),
                }
        except Exception as exc:
            logger.warning(f"获取 Steam package 详情失败: {format_exception(exc)} (pid={pid})")
            return None

    async def fetch_package_price(self, packageid, country="CN"):
        """package/sub 当前价（与 appdetails 价格 dict 同构）。"""
        info = await self.fetch_package_details(packageid, country)
        if not info:
            return None
        price = info.get("price") or {}
        final = price.get("final")
        if final is None:
            return None
        try:
            final = float(final) / 100.0
        except (TypeError, ValueError):
            return None
        initial = price.get("initial")
        try:
            regular = float(initial) / 100.0 if initial is not None else final
        except (TypeError, ValueError):
            regular = final
        return {
            "currency": price.get("currency"),
            "current_price": final,
            "current_regular": regular if regular >= final else final,
            "cut": int(price.get("discount_percent") or 0),
            "is_package": True,
        }


    async def fetch_edition_prices(self, appid, country="CN"):
        """读取 Steam appdetails 的 package_groups，解析本体/豪华版等套餐价。

        返回 [{"name", "price", "regular", "cut", "currency", "packageid"}]
        （按价格升序；无套餐数据时返回 []）
        """
        gid = str(appid).strip()
        if not gid.isdigit():
            return []
        url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&cc={str(country).lower()}&l=schinese"
        try:
            async with shared_httpx_client(proxy=None, timeout=15, follow_redirects=False) as client:
                response = await client.get(url)
                response.raise_for_status()
                payload = response.json().get(gid, {})
                data = payload.get("data") if payload.get("success") else None
                if not data:
                    return []
                currency = None
                editions = []
                seen_pkg = set()
                for group in data.get("package_groups") or []:
                    for sub in group.get("subs") or []:
                        pkg = sub.get("packageid")
                        if pkg is None or pkg in seen_pkg:
                            continue
                        seen_pkg.add(pkg)
                        # option_text 通常是「购买 XXX」或版本名
                        name = str(sub.get("option_text") or group.get("title") or "").strip()
                        name = name.replace("Buy ", "").replace("购买", "").strip()
                        price_cents = sub.get("price_in_cents_with_discount")
                        # 有些接口字段是最终价（已含折扣）
                        if price_cents is None:
                            price_obj = sub.get("price") or {}
                            if isinstance(price_obj, dict):
                                price_cents = price_obj.get("final")
                                currency = currency or price_obj.get("currency")
                                regular = price_obj.get("initial")
                                cut = price_obj.get("discount_percent") or 0
                            else:
                                continue
                        else:
                            regular = sub.get("price_in_cents")
                            cut = sub.get("percent_savings") or 0
                        if price_cents is None:
                            continue
                        try:
                            price = float(price_cents) / 100.0
                            reg = (float(regular) / 100.0) if regular not in (None, 0, "0") else price
                            cut_v = int(cut or 0)
                        except (TypeError, ValueError):
                            continue
                        if not name:
                            name = f"套餐 {pkg}"
                        # option_text 常夹带折扣 HTML，剥掉
                        name = re.sub(r"<[^>]+>", "", name)
                        name = re.sub(r"\s+", " ", name).strip(" -–—")
                        name = re.sub(r"\s*-\s*¥\s*[\d.]+\s*$", "", name).strip() or f"套餐 {pkg}"
                        editions.append({
                            "name": name,
                            "price": price,
                            "regular": reg if reg >= price else price,
                            "cut": cut_v,
                            "currency": currency,
                            "packageid": pkg,
                        })
                # 去重同名取更低价，再按价格排序
                by_name = {}
                for ed in editions:
                    key = ed["name"].casefold()
                    old = by_name.get(key)
                    if old is None or ed["price"] < old["price"]:
                        by_name[key] = ed
                ordered = sorted(by_name.values(), key=lambda x: x["price"])
                # 补币种（从本体 price_overview）
                if not any(e.get("currency") for e in ordered):
                    po = data.get("price_overview") or {}
                    cur = po.get("currency")
                    for e in ordered:
                        e["currency"] = cur
                return ordered
        except Exception as exc:
            logger.warning(f"获取 Steam 版本套餐价失败: {format_exception(exc)} (appid={gid})")
            return []

    async def fetch_edition_prices_multi(self, appid, countries):
        """多区套餐价：按 packageid/名称合并各区 package_groups。

        返回 [{"name", "packageid", "regions": {CC: {price, regular, cut, currency}}}]
        排序：主区价格升序（无主区数据时按名称）。
        """
        countries = [str(c or "").upper() for c in (countries or []) if str(c or "").strip()]
        if not countries:
            return []
        primary = countries[0]
        by_key = {}
        order_keys = []
        for cc in countries:
            try:
                eds = await self.fetch_edition_prices(appid, cc)
            except Exception as e:
                logger.warning(f"[套餐] {cc} 区查询失败 appid={appid}: {e}")
                eds = []
            for ed in eds or []:
                key = str(ed.get("packageid") or ed.get("name") or "").casefold()
                if not key:
                    continue
                if key not in by_key:
                    by_key[key] = {
                        "name": ed.get("name"),
                        "packageid": ed.get("packageid"),
                        "regions": {},
                    }
                    order_keys.append(key)
                by_key[key]["regions"][cc] = {
                    "price": ed.get("price"),
                    "regular": ed.get("regular"),
                    "cut": ed.get("cut"),
                    "currency": ed.get("currency"),
                }
                if not by_key[key]["name"]:
                    by_key[key]["name"] = ed.get("name")

        def _sort_key(k):
            info = by_key[k]
            p = (info.get("regions") or {}).get(primary) or {}
            price = p.get("price")
            try:
                return (0 if price is not None else 1, float(price or 0), str(info.get("name") or ""))
            except (TypeError, ValueError):
                return (1, 0, str(info.get("name") or ""))

        order_keys.sort(key=_sort_key)
        return [by_key[k] for k in order_keys]

    async def get_chinese_game_name(self, gameid, fallback_name=None):
        '''
        优先通过 Steam 商店API获取游戏中文名（l=schinese），若无则返回英文名（l=en），最后才返回 fallback_name 或“未知游戏”
        '''
        if not gameid:
            return fallback_name or "未知游戏"
        gid = str(gameid)
        if gid in self._game_name_cache:
            cached = self._game_name_cache[gid]
            # get_game_names 会缓存 (name_zh, name_en) 元组，需取中文名
            if isinstance(cached, tuple):
                return cached[0] if cached[0] else (cached[1] if len(cached) > 1 else "未知游戏")
            return cached
        # 优先查中文名（l=schinese），再查英文名（l=en）
        url_zh = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=schinese"
        url_en = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=en"
        try:
            async with shared_httpx_client(proxy=None, timeout=10, follow_redirects=False) as client:
                # 查中文名
                resp_zh = await client.get(url_zh)
                data_zh = resp_zh.json()
                info_zh = data_zh.get(gid, {}).get("data", {})
                name_zh = info_zh.get("name")
                if name_zh:
                    self._game_name_cache[gid] = name_zh
                    return name_zh
                # 查英文名
                resp_en = await client.get(url_en)
                data_en = resp_en.json()
                info_en = data_en.get(gid, {}).get("data", {})
                name_en = info_en.get("name")
                if name_en:
                    self._game_name_cache[gid] = name_en
                    return name_en
        except Exception as e:
            logger.debug(f"获取游戏名失败: {e} (gameid={gid})")
        # 不缓存 fallback，让下次还能重试
        return fallback_name or "未知游戏"

    async def get_game_names(self, gameid, fallback_name=None):
        '''
        返回 (中文名, 英文名)，如无则 fallback_name 或 "未知游戏"
        '''
        if not gameid:
            return (fallback_name or "未知游戏", fallback_name or "未知游戏")
        gid = str(gameid)
        if gid in self._game_name_cache:
            cached = self._game_name_cache[gid]
            if isinstance(cached, tuple):
                return cached
            else:
                return (cached, cached)
        url_zh = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=schinese"
        url_en = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l=en"
        name_zh = name_en = fallback_name or "未知游戏"
        try:
            async with shared_httpx_client(proxy=None, timeout=10, follow_redirects=False) as client:
                resp_zh = await client.get(url_zh)
                data_zh = resp_zh.json()
                info_zh = data_zh.get(gid, {}).get("data", {})
                name_zh = info_zh.get("name") or name_zh
                resp_en = await client.get(url_en)
                data_en = resp_en.json()
                info_en = data_en.get(gid, {}).get("data", {})
                name_en = info_en.get("name") or name_en
        except Exception as e:
            logger.debug(f"获取游戏名失败: {e} (gameid={gid})")
        self._game_name_cache[gid] = (name_zh, name_en)
        return (name_zh, name_en)

    async def get_game_cover_url(self, gameid, force_update=False):
        '''
        获取游戏封面图本地路径（优先小图，失败自动尝试日文/英文区域），自动缓存到本地，定期刷新
        force_update: True 时强制重新下载覆盖本地
        '''
        if not gameid:
            return None
        gid = str(gameid)
        if not gid.isdigit():
            return None
        cover_dir = os.path.join(self.data_dir, "covers")
        os.makedirs(cover_dir, exist_ok=True)
        cover_path = os.path.join(cover_dir, f"{gid}.jpg")
        # 定期刷新周期（秒），如30天
        refresh_interval = 30 * 24 * 3600
        need_refresh = force_update
        # 判断本地缓存是否需要刷新
        if os.path.exists(cover_path) and not force_update:
            last_mtime = os.path.getmtime(cover_path)
            if time.time() - last_mtime > refresh_interval:
                need_refresh = True
            else:
                return cover_path
        # 其它本地缓存目录兜底（竖版/横版/图标/最近下载）
        if not force_update:
            data_dir = getattr(self, "data_dir", "") or ""
            for rel in (
                f"covers_v/{gid}.jpg",
                f"covers_h/{gid}.jpg",
                f"covers/{gid}.jpg",
                f"covers/{gid}.png",
                f"covers_multi/{gid}.jpg",
                f"game_icons/{gid}.jpg",
                f"game_icons/{gid}.png",
            ):
                p = os.path.join(data_dir, rel) if data_dir else ""
                if p and os.path.exists(p) and os.path.getsize(p) > 1000:
                    try:
                        os.makedirs(cover_dir, exist_ok=True)
                        if not os.path.exists(cover_path):
                            import shutil
                            shutil.copy2(p, cover_path)
                    except Exception:
                        pass
                    return p if os.path.exists(p) else cover_path
            # 本地资料库已存的 header URL → 直接 CDN 下载，避免商店 API
            try:
                from ...infrastructure.persistence import local_store as ls
                if data_dir:
                    conn = ls.get_store(data_dir)
                    row = conn.execute(
                        "SELECT header_image_url FROM games WHERE appid=?", (gid,)
                    ).fetchone()
                    if row and row["header_image_url"]:
                        url = rewrite_steam_cdn_url(str(row["header_image_url"]))
                        if await self._download_cover_to(url, cover_path):
                            return cover_path
            except Exception:
                pass
        # 先查缓存
        if not need_refresh and hasattr(self, "_game_cover_cache") and gid in self._game_cover_cache:
            return self._game_cover_cache[gid]
        # CDN 硬编码候选：不依赖 appdetails
        try:
            for cand in steam_cover_candidates(gid):
                if await self._download_cover_to(cand, cover_path):
                    return cover_path
        except Exception as e:
            logger.debug(f"[cover] CDN 候选失败 gid={gid}: {e}")
        # 商店冷却时不打 appdetails（同模块函数，勿再 import）
        try:
            if steam_store_blocked():
                return cover_path if os.path.exists(cover_path) else None
        except Exception:
            pass
        # 多区域尝试
        lang_list = ["schinese", "japanese", "en"]
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=10, follow_redirects=False) as client:
                for lang in lang_list:
                    url = f"{self.STEAM_STORE_BASE}/api/appdetails?appids={gid}&l={lang}"
                    resp = await client.get(url)
                    if resp.status_code != 200:
                        logger.warning(f"获取游戏封面API失败: HTTP {resp.status_code} (gameid={gid}, lang={lang})")
                        continue
                    data = resp.json()
                    info = data.get(gid, {}).get("data", {})
                    header_img = info.get("header_image")
                    if not header_img:
                        logger.info(f"未找到游戏封面字段 header_image (gameid={gid}, lang={lang})，API返回data: {repr(info)[:200]}")
                        continue
                    # 新版资源路径为 .../{hash}/header.jpg，旧 replace 无效
                    small_img = header_img.replace("_header.jpg", "_capsule_184x69.jpg")
                    candidates = []
                    if small_img and small_img != header_img:
                        candidates.append(small_img)
                    cap = info.get("capsule_image") or info.get("capsule_imagev5")
                    if cap:
                        candidates.append(cap)
                    candidates.append(header_img)
                    img_resp = None
                    for cand in candidates:
                        try:
                            rr = await client.get(cand)
                        except Exception as de:
                            logger.warning(f"封面图片请求异常: {de} url={cand} (gameid={gid}, lang={lang})")
                            continue
                        if rr.status_code == 200 and rr.content:
                            img_resp = rr
                            break
                        logger.warning(f"封面图片下载失败: HTTP {rr.status_code} url={cand} (gameid={gid}, lang={lang})")
                    if img_resp is not None:
                        with open(cover_path, "wb") as f:
                            f.write(img_resp.content)
                        return cover_path
        except Exception as e:
            logger.warning(f"获取/缓存游戏封面异常: {e} (gameid={gid})")
        # 如果下载失败且本地有旧图，兜底返回旧图
        if os.path.exists(cover_path):
            return cover_path
        return None

    async def _download_cover_to(self, url: str, path: str) -> bool:
        if not url:
            return False
        url = rewrite_steam_cdn_url(str(url))
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=10, follow_redirects=True) as client:
                r = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            if r.status_code == 200 and r.content and len(r.content) > 200:
                with open(path, "wb") as f:
                    f.write(r.content)
                return True
        except Exception:
            return False
        return False
        return False
