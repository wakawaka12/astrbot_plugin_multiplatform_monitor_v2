"""IsThereAnyDeal 客户端：游戏搜索、当前价格与历史最低价。"""
from dataclasses import dataclass
from html import unescape
from typing import Any
import re
import time

import httpx

try:
    from ...shared.logging import logger
except ImportError:
    import logging
    logger = logging.getLogger(__name__)
try:
    from ...shared.network import httpx_client_kwargs, shared_httpx_client
except ImportError:
    from contextlib import asynccontextmanager

    def httpx_client_kwargs(proxy=None):
        return {'proxy': proxy} if proxy else {}

    @asynccontextmanager
    async def shared_httpx_client(proxy=None, timeout=15.0, follow_redirects=False):
        client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=follow_redirects,
            **(httpx_client_kwargs(proxy) or {}),
        )
        try:
            yield client
        finally:
            await client.aclose()

try:
    from .steam import note_steam_store_status, steam_store_blocked
except ImportError:
    def steam_store_blocked():
        return False

    def note_steam_store_status(status_code, *, label=""):
        return None


@dataclass
class ITADGame:
    id: str
    title: str
    url: str = ""
    slug: str = ""
    image: str = ""
    appid: str = ""
    # Steam 商店类型：game / dlc / music / ...（未知时为空）
    content_type: str = ""
    # 预解析中文名，避免展示时重复请求
    title_zh: str = ""


class ITADClient:
    BASE_URL = "https://api.isthereanydeal.com"
    # 连续失败熔断，避免愿望单/查价把共享池拖死
    _fail_streak = 0
    _circuit_until = 0.0

    def __init__(self, api_key: str = "", proxy=None, base_url: str = ""):
        self.api_key = (api_key or "").strip()
        self.proxy = proxy
        self.base_url = (base_url or self.BASE_URL).rstrip("/")

    def _itad_blocked(self) -> bool:
        return time.time() < float(getattr(ITADClient, "_circuit_until", 0) or 0)

    def _note_itad_fail(self, path: str, exc) -> None:
        ITADClient._fail_streak = int(getattr(ITADClient, "_fail_streak", 0)) + 1
        streak = ITADClient._fail_streak
        if streak >= 5:
            ITADClient._circuit_until = time.time() + 300
            ITADClient._fail_streak = 0
            logger.warning(f"ITAD 熔断 5 分钟（连续失败 {streak}） last={path} {type(exc).__name__}: {exc!s}")
        else:
            logger.warning(f"ITAD 请求失败 {path} streak={streak}: {type(exc).__name__}: {exc!s}")

    def _note_itad_ok(self) -> None:
        ITADClient._fail_streak = 0

    async def _get(self, path: str, params: dict[str, Any]):
        if not self.api_key:
            return None
        if self._itad_blocked():
            return None
        params = {**params, "key": self.api_key}
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=8, follow_redirects=True) as client:
                response = await client.get(f"{self.base_url}{path}", params=params)
                response.raise_for_status()
                self._note_itad_ok()
                return response.json()
        except Exception as exc:
            self._note_itad_fail(path, exc)
            # 代理失败再试一次直连（若配置了代理）
            if self.proxy:
                try:
                    async with shared_httpx_client(proxy=None, timeout=8, follow_redirects=True) as client:
                        response = await client.get(f"{self.base_url}{path}", params=params)
                        response.raise_for_status()
                        self._note_itad_ok()
                        return response.json()
                except Exception as exc2:
                    self._note_itad_fail(path, exc2)
            return None

    async def _post(self, path: str, body, params: dict[str, Any]):
        if not self.api_key:
            return None
        if self._itad_blocked():
            return None
        params = {**params, "key": self.api_key}
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=8, follow_redirects=True) as client:
                response = await client.post(f"{self.base_url}{path}", json=body, params=params)
                response.raise_for_status()
                self._note_itad_ok()
                return response.json()
        except Exception as exc:
            self._note_itad_fail(path, exc)
            return None

    async def _parse_search_payload(self, payload, limit: int = 6) -> list[ITADGame]:
        if not isinstance(payload, list):
            return []
        result = []
        for item in payload[:limit]:
            if not isinstance(item, dict):
                continue
            game_id = str(item.get("id") or item.get("gameId") or "")
            title = item.get("title") or item.get("name") or ""
            assets = item.get("assets") if isinstance(item.get("assets"), dict) else {}
            image = assets.get("boxart") or assets.get("banner600") or assets.get("banner") or ""
            if game_id and title:
                result.append(ITADGame(game_id, title, item.get("url", ""), item.get("slug", ""), image))
        return result

    async def _steam_storesearch(self, query: str, language: str = "english", limit: int = 6):
        """只走 storesearch API，避免空结果时被 HTML 页的无关条目顶掉。"""
        if steam_store_blocked():
            return []
        # 英文查询用 cc=us，国区索引英文标题命中率低
        cc = "us" if language == "english" else "cn"
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    "https://store.steampowered.com/api/storesearch/",
                    params={"term": query, "l": language, "cc": cc},
                )
                note_steam_store_status(response.status_code, label="storesearch")
                if response.status_code == 403:
                    logger.warning("Steam storesearch 403: term=%s", query)
                    return []
                response.raise_for_status()
                payload = response.json()
                items = payload.get("items", []) if isinstance(payload, dict) else []
                if isinstance(items, list) and items:
                    return items[:limit]
                return []
        except Exception as exc:
            logger.warning("Steam storesearch 失败: %s", exc)
            return []

    async def _steam_search_html(self, query: str, language: str = "english", limit: int = 6):
        """商店搜索页兜底；国区成人内容经常被过滤，调用方需再做标题相关度校验。"""
        if steam_store_blocked():
            return []
        cc = "us" if language == "english" else "cn"
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=True) as client:
                page = await client.get(
                    "https://store.steampowered.com/search/results/",
                    params={"term": query, "l": language, "cc": cc, "count": limit, "json": 1},
                    headers={"Accept": "application/json"},
                )
                note_steam_store_status(page.status_code, label="search_html")
                if page.status_code == 403:
                    logger.warning("Steam 搜索页 403: term=%s", query)
                    return []
                page.raise_for_status()
                page_payload = page.json()
                html = page_payload.get("results_html", "") if isinstance(page_payload, dict) else ""
                results = self._parse_steam_search_html(html, limit)
                if results:
                    return results

                page = await client.get(
                    "https://store.steampowered.com/search/",
                    params={"term": query, "l": language, "cc": cc},
                    headers={"Accept": "text/html,application/xhtml+xml"},
                )
                page.raise_for_status()
                return self._parse_steam_search_html(page.text, limit)
        except Exception as exc:
            logger.warning("Steam 搜索页失败: %s", exc)
            return []

    async def _steam_search(self, query: str, language: str = "english", limit: int = 6):
        items = await self._steam_storesearch(query, language, limit)
        if items:
            return items
        return await self._steam_search_html(query, language, limit)

    @staticmethod
    def _parse_steam_search_html(html: str, limit: int) -> list[dict[str, str]]:
        """解析 Steam 搜索结果卡片，兼容属性顺序和 class 扩展。"""
        if not isinstance(html, str) or limit <= 0:
            return []

        results = []
        seen_appids: set[str] = set()
        # 搜索结果通常是指向 /app/<appid>/... 的卡片链接；不要依赖
        # data-ds-appid 位于固定位置，也不要跨卡片寻找标题。
        card_pattern = re.compile(
            r'<a\b(?=[^>]*\bhref=["\'][^"\']*/app/(\d+)(?:/|["\']))[^>]*>([\s\S]*?)</a>',
            re.IGNORECASE,
        )
        element_pattern = re.compile(
            r'<(?P<tag>[a-z][a-z0-9]*)\b(?P<attrs>[^>]*)>(?P<body>[\s\S]*?)</(?P=tag)>',
            re.IGNORECASE,
        )
        image_pattern = re.compile(r'<img\b[^>]*\bsrc=["\']([^"\']+)["\']', re.IGNORECASE)
        for card in card_pattern.finditer(html):
            appid = card.group(1)
            if appid in seen_appids:
                continue
            title_match = None
            for element in element_pattern.finditer(card.group(2)):
                class_match = re.search(r'\bclass=["\']([^"\']*)["\']', element.group("attrs"), re.IGNORECASE)
                if class_match and re.search(r"(?:^|\s)title(?:\s|$)", class_match.group(1), re.IGNORECASE):
                    title_match = element
                    break
            if not title_match:
                continue
            title = re.sub(r"<[^>]+>", " ", title_match.group("body"))
            title = re.sub(r"\s+", " ", unescape(title)).strip()
            if not title:
                continue
            item = {"id": appid, "name": title}
            image_match = image_pattern.search(card.group(2))
            if image_match:
                item["tiny_image"] = unescape(image_match.group(1))
            results.append(item)
            seen_appids.add(appid)
            if len(results) >= limit:
                break
        return results

    async def _steam_english_title(self, appid: str) -> str:
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=True) as client:
                response = await client.get(
                    "https://store.steampowered.com/api/appdetails/",
                    params={"appids": appid, "l": "english", "cc": "cn"},
                )
                response.raise_for_status()
                payload = response.json().get(str(appid), {})
                data = payload.get("data", {}) if payload.get("success") else {}
                return str(data.get("name") or "").strip()
        except Exception as exc:
            logger.warning("Steam 英文标题获取失败 appid=%s: %s", appid, exc)
            return ""

    @staticmethod
    def _edition_keywords() -> tuple[str, ...]:
        return (
            "deluxe edition",
            "ultimate edition",
            "gold edition",
            "complete edition",
            "deluxe",
            "ultimate",
            "goty",
            "豪华版",
            "终极版",
            "黄金版",
            "完全版",
            "年度版",
            "决定版",
        )

    @classmethod
    def _query_wants_edition(cls, query: str) -> bool:
        """用户明确要查豪华版/终极版等时，搜索不再把对应版本当 DLC 丢掉。"""
        q = str(query or "").casefold()
        return any(k in q for k in cls._edition_keywords())

    @classmethod
    def _looks_like_edition(cls, title: str) -> bool:
        t = str(title or "").casefold()
        if not t:
            return False
        return any(k in t for k in (
            "deluxe edition",
            "ultimate edition",
            "gold edition",
            "complete edition",
            "deluxe",
            "ultimate",
            "goty",
            "豪华",
            "终极",
            "黄金",
            "完全版",
            "年度版",
            "决定版",
        ))

    @staticmethod
    def _looks_like_dlc(title: str) -> bool:
        """粗判 DLC / 资料片 / 捆绑包 / 道具套组。

        注意：豪华版/终极版等「游戏本体高级套餐」不在此列，
        是否过滤由 _looks_like_hard_dlc + 查询意图共同决定。
        仅靠标题不够：不少 DLC 名称没有 DLC 字样，需再看 Steam type。
        """
        t = str(title or "").casefold()
        if not t:
            return False
        patterns = (
            r"\bdlc\b",
            r"\bsoundtrack\b",
            r"\bseason pass\b",
            r"\bexpansion pack\b",
            r"\bexpansion\b",
            r"\baddon\b",
            r"\badd-on\b",
            r"\bpack\b",
            r"\bset\b",
            r"\bstarter\b",
            r"\bdemo\b",
            r"\b試用\b",
            r"资料片",
            r"追加",
            r"扩展包",
            r"扩展内容",
            r"原声",
            r"捆绑包",
            r"季票",
            r"套组",
            r"道具",
            r"服装",
            r"机壳",
            r"体验版",
            r"免费试用",
            r"导力器",
            r"啦啦队",
        )
        return any(re.search(p, t) for p in patterns)

    @staticmethod
    def _is_dlc_like_name(title: str, typ: str = "") -> bool:
        """展示/排序用：是否应视为 DLC/套组/演示（而非游戏本体）。"""
        typ_l = str(typ or "").lower()
        if typ_l in {"dlc", "dlc_detail", "music", "episode", "demo", "mod"}:
            return True
        t = str(title or "")
        if ITADClient._looks_like_dlc(t):
            return True
        # 名称含「 - xxx」且带套组/包/服装等尾巴
        if re.search(r"\s[-–—]\s*", t) and re.search(r"套组|包|服装|道具|Season|Pass|Set|DLC", t, re.I):
            return True
        return False

    @classmethod
    def _skip_as_dlc(cls, title: str, query: str = "", *, edition_ok: bool = False) -> bool:
        """是否应把该条当成噪音跳过。

        - 硬 DLC（资料片/季票/原声）总是降级
        - 豪华版等：用户没点名要时降级；点名要时保留
        """
        t = str(title or "")
        if cls._looks_like_dlc(t):
            return True
        if cls._looks_like_edition(t) and not edition_ok:
            return True
        return False

    @staticmethod
    def _query_tokens(query: str) -> list[str]:
        tokens = re.findall(r"[0-9a-zA-Z]+|[\u4e00-\u9fff]+", str(query or "").casefold())
        return [token for token in tokens if len(token) >= 2 or token.isdigit()]

    @staticmethod
    def _token_in_title(token: str, haystack: str) -> bool:
        if re.fullmatch(r"[0-9a-z]+", token):
            return re.search(rf"(?<![0-9a-z]){re.escape(token)}(?![0-9a-z])", haystack) is not None
        return token in haystack

    @classmethod
    def _title_matches_query(cls, title: str, query: str) -> bool:
        """标题需覆盖查询词中足够多的有效 token，避免 Steamy 糊到 steam。"""
        tokens = cls._query_tokens(query)
        if not tokens:
            return bool(str(title or "").strip())
        haystack = str(title or "").casefold()
        if not haystack:
            return False
        if haystack == str(query or "").casefold().strip():
            return True
        matched = sum(1 for token in tokens if cls._token_in_title(token, haystack))
        if len(tokens) == 1:
            # 单 token：允许前缀匹配（Absolum → Absolum: …）
            return matched == 1 or haystack.startswith(tokens[0]) or tokens[0].startswith(haystack[:4])
        return matched >= max(2, (len(tokens) + 1) // 2)

    def _filter_steam_items(self, items, query: str, limit: int) -> list[dict]:
        result = []
        seen: set[str] = set()
        dlc_backup = []
        edition_ok = self._query_wants_edition(query)
        for item in items or []:
            if not isinstance(item, dict):
                continue
            appid = str(item.get("id") or "")
            title = str(item.get("name") or "").strip()
            if not appid or appid in seen or not title:
                continue
            if not self._title_matches_query(title, query):
                continue
            seen.add(appid)
            # Steam storesearch: type=app/dlc/dlc_detail...
            typ = str(item.get("type") or "").lower()
            is_hard_dlc = typ in ("dlc", "dlc_detail", "music") or self._looks_like_dlc(title)
            is_edition = self._looks_like_edition(title)
            # 点名查豪华版：把命中查询关键词的版本排前面
            if edition_ok and is_edition and not is_hard_dlc:
                result.insert(0, item)
                if len(result) > limit:
                    result = result[:limit]
                continue
            if is_hard_dlc or (is_edition and not edition_ok):
                dlc_backup.append(item)
                continue
            result.append(item)
            if len(result) >= limit:
                break
        # 本体不够时再用 DLC/版本补齐，避免完全搜不到
        if len(result) < limit:
            for item in dlc_backup:
                if item not in result:
                    result.append(item)
                if len(result) >= limit:
                    break
        return result

    # 中文系列名 → 英文商店检索词（补全老版/英文上架条目）
    _SERIES_ALIASES = {
        "空之轨迹": ("Trails in the Sky", "Sora no Kiseki"),
        "零之轨迹": ("Trails from Zero",),
        "碧之轨迹": ("Trails to Azure",),
        "闪之轨迹": ("Trails of Cold Steel",),
        "创之轨迹": ("Trails into Reverie",),
        "黎之轨迹": ("Trails through Daybreak", "Kuro no Kiseki"),
        "界之轨迹": ("Kai no Kiseki", "Trails beyond the Horizon"),
        "英雄传说": ("The Legend of Heroes",),
        "伊苏": ("Ys",),
        "最终幻想": ("Final Fantasy",),
        "勇者斗恶龙": ("Dragon Quest",),
        "怪物猎人": ("Monster Hunter",),
        "女神异闻录": ("Persona",),
        "黑暗之魂": ("Dark Souls",),
        "只狼": ("Sekiro",),
        "博德之门": ("Baldur's Gate",),
        "巫师": ("The Witcher",),
        "上古卷轴": ("The Elder Scrolls", "Skyrim"),
        "生化危机": ("Resident Evil", "Biohazard"),
    }

    @classmethod
    def _series_aliases(cls, query: str) -> list[str]:
        q = str(query or "")
        if not q:
            return []
        aliases: list[str] = []
        for zh, ens in cls._SERIES_ALIASES.items():
            if zh in q:
                for e in ens:
                    if e not in aliases:
                        aliases.append(e)
        return aliases

    @staticmethod
    def _query_variants(query: str) -> list[str]:
        """生成商店搜索词变体：中英/数字之间补空格，兼容「空之轨迹2nd」→「空之轨迹 the 2nd」。"""
        q = str(query or "").strip()
        if not q:
            return []
        variants: list[str] = []

        def _add(x: str):
            x = re.sub(r"\s+", " ", str(x or "")).strip()
            if x and x not in variants:
                variants.append(x)

        _add(q)
        spaced = re.sub(r"([一-鿿])([0-9a-zA-Z]+)", r"\1 \2", q)
        spaced = re.sub(r"([0-9a-zA-Z]+)([一-鿿])", r"\1 \2", spaced)
        _add(spaced)
        m = re.match(r"^(.*[一-鿿]+)\s*([0-9a-zA-Z].*)$", q)
        if m:
            base, tail = m.group(1).strip(), m.group(2).strip()
            # 重制版常见命名：XXX the 2nd / XXX SC
            _add(f"{base} the {tail}")
            _add(f"{base} {tail}")
            if tail.lower() in {"2nd", "2", "ii"}:
                _add(f"{base} the 2nd")
                _add(f"{base} SC")
            if tail.lower() in {"1st", "1", "i", "fc"}:
                _add(f"{base} the 1st")
        # 纯中文系列名：主动补重制版 1st/2nd，避免只列出英文老版
        if re.fullmatch(r"[一-鿿]+", q):
            for suffix in ("the 1st", "the 2nd", "1st", "2nd"):
                _add(f"{q} {suffix}")
        return variants

    async def _lookup_steam_items(self, query: str, limit: int) -> list[dict]:
        """中文/英文多路检索并合并。

        - 中文变体（补空格 / the Nnd）
        - 系列英文别名（空之轨迹 → Trails in the Sky），补全老版等英文条目
        - 结果排序：中文名优先，其次本体，DLC/套组仍带标记保留
        """
        edition_ok = self._query_wants_edition(query)
        cjk_query = bool(re.search(r"[一-鿿]", str(query or "")))
        variants = self._query_variants(query) or [query]
        aliases = self._series_aliases(query)
        # 系列查询时放宽数量，尽量列全（英文老版也要进来）
        fetch_limit = max(int(limit or 6), 20 if aliases else int(limit or 6))

        collected: list[dict] = []
        seen: set[str] = set()

        def _absorb(items: list[dict]):
            for item in items or []:
                aid = str(item.get("id") or "")
                if aid and aid not in seen:
                    seen.add(aid)
                    collected.append(item)

        if cjk_query:
            for term in variants:
                got = await self._steam_storesearch(term, "schinese", fetch_limit)
                if not got:
                    # 偶发空结果时重试一次（商店接口不稳定）
                    got = await self._steam_storesearch(term, "schinese", fetch_limit)
                _absorb(got)
        for term in variants:
            _absorb(await self._steam_storesearch(term, "english", fetch_limit))
        # 英文系列别名：老三部曲等仅英文上架的条目
        for alias in aliases:
            _absorb(await self._steam_storesearch(alias, "english", fetch_limit))

        # 标题过滤：中文词 或 英文别名 任一命中即可
        match_terms = list(variants) + list(aliases) + [query]
        def _title_ok(title: str) -> bool:
            t = str(title or "")
            return any(self._title_matches_query(t, term) for term in match_terms if term)

        filtered = []
        dlc_backup = []
        for item in collected:
            if not isinstance(item, dict):
                continue
            title = str(item.get("name") or "").strip()
            if not title or not _title_ok(title):
                continue
            typ = str(item.get("type") or "").lower()
            is_hard_dlc = self._is_dlc_like_name(title, typ)
            is_edition = self._looks_like_edition(title)
            if edition_ok and is_edition and not is_hard_dlc:
                filtered.insert(0, item)
                continue
            if is_hard_dlc or (is_edition and not edition_ok):
                dlc_backup.append(item)
                continue
            filtered.append(item)

        cjk_bits = re.findall(r"[一-鿿]{2,}", str(query or ""))

        def _sort_key(it):
            name = str(it.get("name") or "")
            typ = str(it.get("type") or "").lower()
            hard = 1 if self._is_dlc_like_name(name, typ) else 0
            zh = 0 if re.search(r"[一-鿿]", name) else 1
            zh_hit = sum(1 for b in cjk_bits if b in name)
            # 本体优先：the 1st/2nd 等重制正题略靠前
            remake = 0
            if not hard and re.search(r"(?i)\b(the\s+)?(1st|2nd|first|second)\b", name):
                remake = -1
            if "体验版" in name or re.search(r"(?i)\bdemo\b", name):
                remake = 2
            # 本体 > 重制正题 > 中文名 > 英文名
            return (hard, remake, -zh_hit, zh, name.casefold())

        filtered.sort(key=_sort_key)
        dlc_backup.sort(key=_sort_key)
        # 系列查询：本体全进，DLC 只留 2 条示意
        if aliases:
            keep_extra = 2 if len(filtered) >= 2 else 4
            filtered = filtered + dlc_backup[:keep_extra]
        else:
            if len(filtered) < fetch_limit:
                filtered = filtered + dlc_backup[: max(0, fetch_limit - len(filtered))]

        if not filtered:
            for language in ("schinese", "english"):
                html_items = []
                hseen = set()
                for term in variants + aliases:
                    for item in await self._steam_search_html(term, language, limit) or []:
                        aid = str(item.get("id") or "")
                        if aid and aid not in hseen:
                            hseen.add(aid)
                            html_items.append(item)
                filtered = [i for i in html_items if _title_ok(i.get("name", ""))]
                if any(not self._skip_as_dlc(i.get("name", ""), edition_ok=edition_ok) for i in filtered):
                    return filtered[:fetch_limit]
        return filtered[:fetch_limit]

    async def search_games(self, query: str, limit: int = 6) -> list[ITADGame]:
        """先通过 Steam 商店解析本地化名称，再用英文标题查询 ITAD。"""
        edition_ok = self._query_wants_edition(query)
        aliases = self._series_aliases(query)
        steam_items = await self._lookup_steam_items(query, max(limit, 8 if aliases else limit))

        # Steam 中文索引可能暂时没有结果；保留 ITAD 直搜作为兜底，避免中文查询完全失败。
        if not steam_items:
            fallback = await self._parse_search_payload(
                await self._get("/games/search/v1", {"title": query, "results": limit}), limit
            )
            matched = [
                game for game in fallback
                if self._title_matches_query(game.title, query)
                and not self._skip_as_dlc(game.title, edition_ok=edition_ok)
            ]
            if not matched:
                matched = [game for game in fallback if self._title_matches_query(game.title, query)]
            for game in matched:
                candidates = self._filter_steam_items(
                    await self._steam_search(game.title, "english", 3), game.title, 3
                )
                if candidates:
                    game.appid = str(candidates[0].get("id") or "")
                    game.image = game.image or candidates[0].get("tiny_image", "")
            return matched[:limit]

        result: list[ITADGame] = []
        seen_keys: set[str] = set()
        cjk_bits = re.findall(r"[一-鿿]{2,}", str(query or ""))
        target_n = max(int(limit or 6), 12 if aliases else int(limit or 6))

        def _title_ok(loc: str, en: str) -> bool:
            """查询词 / 系列别名 / 中文词根 任一命中即保留。"""
            for t in (loc, en):
                if not t:
                    continue
                if self._title_matches_query(t, query):
                    return True
                if cjk_bits and any(b in t for b in cjk_bits):
                    return True
                for a in aliases:
                    if not a:
                        continue
                    if self._title_matches_query(t, a) or a.casefold() in t.casefold():
                        return True
            # lookup 已筛过一轮：系列查询时信任 storesearch 条目
            if aliases and loc:
                return True
            return False

        for item in steam_items:
            if len(result) >= target_n:
                break
            appid = str(item.get("id") or "")
            local_title = str(item.get("name") or "").strip()
            if not appid or not local_title:
                continue
            # 英文名仅在本地名不够判断时才请求（省时，避免每条都打 appdetails）
            english_title = ""
            has_cjk = bool(re.search(r"[一-鿿]", local_title))
            if aliases or has_cjk:
                if not _title_ok(local_title, ""):
                    english_title = await self._steam_english_title(appid)
            else:
                english_title = await self._steam_english_title(appid)

            skip_dlc = self._skip_as_dlc(local_title, edition_ok=edition_ok) or self._skip_as_dlc(
                english_title, edition_ok=edition_ok
            )
            # 系列：DLC 保留并打标；非系列跳过硬 DLC
            if skip_dlc and not aliases:
                continue
            if not _title_ok(local_title, english_title):
                continue

            search_title = local_title or english_title
            # 无 ITAD key 时直接用 Steam appid，避免空请求
            game = None
            if self.api_key:
                itad_games = await self._parse_search_payload(
                    await self._get("/games/search/v1", {"title": english_title or search_title, "results": 3}), 3
                )
                norm = (english_title or search_title).casefold()
                game = next(
                    (cand for cand in itad_games if cand.title.casefold().strip() == norm),
                    None,
                )
                if game is None and itad_games:
                    game = itad_games[0]
            if game is None:
                game = ITADGame(f"steam:{appid}", search_title)

            dedupe_key = game.id or f"steam:{appid}"
            if dedupe_key in seen_keys or appid in {g.appid for g in result}:
                continue
            game.appid = appid
            game.title = local_title or english_title or game.title
            game.image = game.image or item.get("tiny_image", "")
            game.content_type = str(item.get("type") or "").lower()
            game.title_zh = local_title if re.search(r"[一-鿿]", local_title) else game.title_zh
            seen_keys.add(dedupe_key)
            result.append(game)

        # 本体始终排前：DLC/演示后置
        def _res_key(g: ITADGame):
            name = str(g.title or "")
            zh_name = str(getattr(g, "title_zh", "") or "")
            hard = self._is_dlc_like_name(name, getattr(g, "content_type", "")) or self._is_dlc_like_name(zh_name)
            zh = 0 if re.search(r"[一-鿿]", name + zh_name) else 1
            return (1 if hard else 0, zh, name.casefold())

        result.sort(key=_res_key)
        return result[:target_n]

    async def get_prices(self, game_id: str, country: str = "CN") -> dict[str, Any]:
        # ITAD v3 body 为 UUID 数组（官方 schema: array of string/uuid）
        if not game_id:
            return {}
        payload = await self._post("/games/prices/v3", [game_id], {"country": country})
        if isinstance(payload, list) and payload:
            for item in payload:
                if isinstance(item, dict) and item.get("id") == game_id:
                    return item
            # id 不完全一致时退回第一条（ITAD 偶发归一化差异）
            first = payload[0]
            return first if isinstance(first, dict) else {}
        return {}

    async def lookup_by_appid(self, appid) -> dict[str, Any]:
        """用 Steam appid 反查 ITAD 游戏，返回 {id, slug, title} 或 {}。"""
        aid = str(appid or "").strip()
        if not aid.isdigit():
            return {}
        payload = await self._get("/games/lookup/v1", {"appid": aid})
        if not isinstance(payload, dict):
            return {}
        game = payload.get("game") or {}
        if payload.get("found") and isinstance(game, dict) and game.get("id"):
            return game
        return {}

    async def lookup_by_title(self, title) -> dict[str, Any]:
        """用标题反查 ITAD 游戏（含可能关联的 Steam appid 字段时以返回体为准）。"""
        t = str(title or "").strip()
        if not t:
            return {}
        payload = await self._get("/games/lookup/v1", {"title": t})
        if not isinstance(payload, dict):
            return {}
        game = payload.get("game") or {}
        if payload.get("found") and isinstance(game, dict) and game.get("id"):
            return game
        return {}

    async def resolve_game_appid(self, game: ITADGame) -> str:
        """尽量为 ITADGame 补全 Steam appid；已有则原样返回。"""
        aid = str(getattr(game, "appid", "") or "").strip()
        if aid.isdigit():
            return aid
        gid = str(getattr(game, "id", "") or "").strip()
        if gid.startswith("steam:") and gid[6:].isdigit():
            aid = gid[6:]
            game.appid = aid
            return aid
        title = str(getattr(game, "title", "") or "").strip()
        if not title:
            return ""
        # 1) Steam 商店搜索
        for lang, cc in (("schinese", "cn"), ("english", "us")):
            try:
                items = await self._steam_search(title, lang, 3)
            except Exception:
                items = []
            cands = self._filter_steam_items(items, title, 3)
            if cands:
                aid = str(cands[0].get("id") or "")
                if aid.isdigit():
                    game.appid = aid
                    if not game.image:
                        game.image = cands[0].get("tiny_image") or game.image
                    return aid
        return ""

    async def get_history(self, game_id: str, country: str = "CN") -> list[dict[str, Any]]:
        if not game_id:
            return []
        payload = await self._get("/games/history/v2", {"id": game_id, "country": country})
        if isinstance(payload, list):
            return payload
        return []

    async def get_price_summary(self, game_id: str, country: str = "CN") -> dict[str, Any]:
        import asyncio

        current, history = await asyncio.gather(
            self.get_prices(game_id, country), self.get_history(game_id, country)
        )
        current_price = None
        current_regular = None
        currency = None
        cut = None
        steam_store_low = None
        steam_store_low_currency = None
        steam_history_low = None
        steam_history_low_currency = None
        deals = current.get("deals", []) if isinstance(current, dict) else []
        # 优先 Steam 店 deal（与玩家在 Steam 上看到的价一致）
        ordered = []
        for deal in deals:
            if not isinstance(deal, dict):
                continue
            shop = deal.get("shop") or {}
            sname = str(shop.get("name") or "").lower()
            sid = shop.get("id")
            if sid == 61 or "steam" in sname:
                ordered.insert(0, deal)
            else:
                ordered.append(deal)
        for deal in ordered:
            price = deal.get("price") or {}
            regular = deal.get("regular") or {}
            store_low = deal.get("storeLow") or {}
            amount = price.get("amount")
            if amount is None:
                continue
            current_price = float(amount)
            current_regular = float(regular.get("amount") or current_price)
            currency = price.get("currency")
            cut = deal.get("cut")
            try:
                if store_low.get("amount") is not None:
                    steam_store_low = float(store_low.get("amount"))
                    steam_store_low_currency = store_low.get("currency") or currency
            except (TypeError, ValueError):
                pass
            break
        # 只统计 Steam 渠道的历史价（用户不要第三方店铺）
        for item in history:
            if not isinstance(item, dict):
                continue
            shop = item.get("shop") or {}
            sname = str(shop.get("name") or "").lower()
            sid = shop.get("id")
            if not (sid == 61 or "steam" in sname):
                continue
            deal = item.get("deal") or {}
            price = deal.get("price") or {}
            amount = price.get("amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            cur_h = price.get("currency") or currency
            if steam_history_low is None or amount < steam_history_low:
                steam_history_low = amount
                steam_history_low_currency = cur_h
        # Steam 店史低取「Steam 历史价」与「deal.storeLow」中更合理者：优先显式历史价
        if steam_history_low is None:
            steam_history_low = steam_store_low
            steam_history_low_currency = steam_store_low_currency
        history_low = None
        history_low_currency = None
        low_obj = current.get("historyLow") if isinstance(current, dict) else None
        if isinstance(low_obj, dict):
            low_all = low_obj.get("all") or {}
            try:
                history_low = float(low_all.get("amount"))
            except (TypeError, ValueError):
                history_low = None
            if not currency:
                currency = low_all.get("currency")
            history_low_currency = low_all.get("currency") or currency
        lowest = None
        for item in history:
            if not isinstance(item, dict):
                continue
            deal = item.get("deal") or {}
            price = deal.get("price") or {}
            amount = price.get("amount")
            try:
                amount = float(amount)
            except (TypeError, ValueError):
                continue
            lowest = amount if lowest is None else min(lowest, amount)
        return {
            "current": current,
            "current_price": current_price,
            "current_regular": current_regular,
            "currency": currency,
            "cut": cut,
            # Steam 渠道史低（查价展示用这个）
            "steam_history_low": steam_history_low,
            "steam_history_low_currency": steam_history_low_currency,
            "steam_store_low": steam_store_low,
            "steam_store_low_currency": steam_store_low_currency,
            # 以下为全渠道，仅内部/兼容，不优先展示
            "history_low": history_low,
            "history_low_currency": history_low_currency,
            "lowest": lowest,
            "history": history,
        }
