"""查价 / 序号选择 / 多区价格应用服务（从主插件拆出）。

运行时由 SteamStatusMonitorV3 多继承挂载；本文件不注册 AstrBot 指令。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
import traceback
from datetime import date, datetime

from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import Image, Plain

from ...infrastructure.clients.steam import format_sale_end_line, steam_store_blocked, steam_store_guard_msg
from ...infrastructure.persistence import local_store as lstore
from ...shared.logging import logger
from ...shared.network import httpx_client_kwargs, shared_httpx_client
from ...shared.utils.price import extract_price_query, summary_to_cny, to_cny
from ...shared.utils.cache_age import format_cache_age


class GamePriceServiceMixin:
    """Steam 查价、候选缓存、翻译与多区价格收集。"""

    def _store_conn(self):
        try:
            return lstore.get_store(getattr(self, "data_dir", "") or "")
        except Exception as e:
            logger.debug(f"[local_store] open fail: {e}")
            return None

    def _appid_cache_path(self) -> str:
        return os.path.join(getattr(self, "data_dir", "") or "", "price_appid_cache.json")

    def _load_appid_cache(self) -> dict:
        path = self._appid_cache_path()
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f) or {}
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
        return {}

    def _save_appid_cache_entry(self, title: str, appid: str) -> None:
        title = str(title or "").strip()
        appid = str(appid or "").strip()
        if not title or not appid.isdigit():
            return
        try:
            data = self._load_appid_cache()
            data[title.casefold()] = appid
            data[re.sub(r"\s+", "", title).casefold()] = appid
            path = self._appid_cache_path()
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass
        conn = self._store_conn()
        if conn is not None:
            try:
                lstore.set_search_map(conn, title, appid, source="price_cache")
                lstore.upsert_game(conn, appid=appid, name=title)
                conn.commit()
            except Exception as e:
                logger.debug(f"[local_store] search_map save fail: {e}")

    def _cached_appid_for_title(self, title: str) -> str:
        title = str(title or "").strip()
        if not title:
            return ""
        conn = self._store_conn()
        if conn is not None:
            try:
                aid = lstore.get_search_appid(conn, title)
                if aid.isdigit():
                    return aid
            except Exception:
                pass
        data = self._load_appid_cache()
        return str(
            data.get(title.casefold())
            or data.get(re.sub(r"\s+", "", title).casefold())
            or ""
        ).strip()

    async def _ensure_game_appid(self, game):
        """确保 game.appid 可用：已有 → steam:id 前缀 → 本地缓存 → 反查。"""
        aid = str(getattr(game, "appid", "") or "").strip()
        if aid.isdigit():
            return game
        gid = str(getattr(game, "id", "") or "")
        if gid.startswith("steam:") and gid[6:].isdigit():
            try:
                game.appid = gid[6:]
            except Exception:
                pass
            return game
        title = str(getattr(game, "title", "") or getattr(game, "title_zh", "") or "")
        cached = self._cached_appid_for_title(title)
        if cached.isdigit():
            try:
                game.appid = cached
            except Exception:
                pass
            return game
        await self._safe_call(
            self.ITAD_CLIENT.resolve_game_appid(game),
            timeout=12.0, default="", label=f"resolve_appid {title}",
        )
        aid = str(getattr(game, "appid", "") or "").strip()
        if aid.isdigit():
            self._save_appid_cache_entry(title, aid)
        return game

    async def _ensure_game_itad_id(self, game):
        """确保 game.id 是 ITAD UUID（避免 steam:appid 打价格接口 400）。"""
        await self._ensure_game_appid(game)
        gid = str(getattr(game, "id", "") or "").strip()
        if gid and not gid.startswith("steam:") and len(gid) >= 30:
            return game
        aid = str(getattr(game, "appid", "") or "").strip()
        if aid.isdigit():
            mapped = await self._safe_call(
                self.ITAD_CLIENT.lookup_by_appid(aid),
                timeout=8.0, default={}, label=f"ITAD map {aid}",
            ) or {}
            if mapped.get("id"):
                try:
                    game.id = str(mapped.get("id"))
                except Exception:
                    pass
        return game

    async def _enrich_search_games(self, games):
        """搜索结果补全 appid，并稳定排序。"""
        if not games:
            return games
        for g in games:
            try:
                await self._ensure_game_appid(g)
            except Exception as e:
                logger.debug(f"[查价] 候选补 appid 失败 {getattr(g, 'title', None)}: {e}")
            await asyncio.sleep(0.35)

        def _key(g):
            aid = str(getattr(g, "appid", "") or "")
            title = str(getattr(g, "title", "") or "")
            zh = str(getattr(g, "title_zh", "") or "")
            ctype = str(getattr(g, "content_type", "") or "").lower()
            name = f"{title} {zh}"
            is_pkg = ctype in {"sub", "package", "bundle", "dlc", "4"} or self.ITAD_CLIENT._looks_like_edition(name)
            return (
                0 if aid.isdigit() else 1,
                1 if is_pkg else 0,
                0 if re.search(r"[一-鿿]", name) else 1,
                title.casefold(),
            )

        return sorted(games, key=_key)

    async def _game_price_impl(self, event: AstrMessageEvent, query: str):
        async for r in self._steam_price_cmd_impl(event, query):
            yield r

    async def _price_short_impl(self, event: AstrMessageEvent, query: str = ""):
        '''价格查询：/price 游戏名'''
        if not query or not query.strip():
            yield event.plain_result("用法：/price <游戏名或Steam链接>\n例：/price 艾尔登法环")
            return
        async for r in self._steam_price_cmd_impl(event, query):
            yield r

    async def _px_short_impl(self, event: AstrMessageEvent, query: str = ""):
        '''价格快捷查询：/px 游戏名'''
        if not query or not query.strip():
            yield event.plain_result("用法：/px <游戏名>\n例：/px 黑神话：悟空")
            return
        async for r in self._steam_px_impl(event, query):
            yield r

    @staticmethod
    def _steam_search_session_key(event: AstrMessageEvent) -> str:
        """Return a stable key for both group and private conversations."""
        origin = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if origin:
            return origin
        session_id = str(event.get_session_id() or "").strip()
        return session_id or "default"

    async def _steam_price_selection_impl(self, event: AstrMessageEvent):
        """仅当【引用机器人候选列表】并回复纯数字时，才认作查价序号。"""
        if self.steam_guard_active():
            blk = self._steam_guard_block_msg()
            if blk:
                yield event.plain_result(blk)
                return
        session_key = self._steam_search_session_key(event)
        raw_msg = str(event.get_message_str() or "").strip()
        if not raw_msg:
            return

        # 1) 必须存在引用/回复段
        quoted_text = ""
        has_reply = False
        m_cq = re.match(r"^\[CQ:reply,[^\]]*\]\s*", raw_msg)
        m_ref = re.match(r"^\[引用消息(.*?)\]\s*", raw_msg)
        if m_cq or m_ref:
            has_reply = True
            if m_ref:
                quoted_text = m_ref.group(1) or ""
        # 消息链里有 Reply 组件时也算引用
        if not has_reply:
            try:
                segs = getattr(event, "get_messages", lambda: None)() or []
                for seg in segs:
                    name = type(seg).__name__.lower()
                    if name == "reply":
                        has_reply = True
                        quoted_text = str(
                            getattr(seg, "message_str", "")
                            or getattr(seg, "content", "")
                            or getattr(seg, "message", "")
                            or ""
                        )
                        break
            except Exception:
                pass

        if not has_reply:
            # 未引用：即使整条是数字也不认（防聊天误触）
            if re.fullmatch(r"[1-9]\d?", re.sub(r"[\s、,，]", "", raw_msg)[:2] or ""):
                pass
            return

        # 2) 引用内容应像「候选列表」，防止引用无关消息后发数字
        q = quoted_text or raw_msg
        if not re.search(r"请回复序号|请引用|找到多个匹配|序号约|候选", q):
            # CQ:reply 往往拿不到被引用正文，则放宽为：有待选缓存即可
            if not self._steam_search_pending.get(session_key):
                return

        # 3) 用户本条正文（去掉引用壳）必须是纯序号
        reply = raw_msg
        if m_cq:
            reply = raw_msg[m_cq.end():].strip()
        elif m_ref:
            reply = raw_msg[m_ref.end():].strip()
        else:
            # Reply 组件场景：message_str 可能只有数字
            reply = re.sub(r"^\[CQ:reply,[^\]]*\]\s*", "", raw_msg).strip()
            reply = re.sub(r"^\[引用消息.*?\]\s*", "", reply).strip()
        if not re.fullmatch(r"[1-9]\d?(?:\s*[、,，]\s*[1-9]\d?|\s+[1-9]\d?)*", reply):
            return

        if not self._steam_search_pending.get(session_key):
            ttl_min = int(round(self._price_cache_ttl() / 60.0)) or 3
            yield event.plain_result(
                f"查价候选序号已失效，请重新发送 /price 游戏名。\n"
                f"需【引用】机器人的候选列表消息，再回复数字；有效期约 {ttl_min} 分钟。"
            )
            return

        event.stop_event()
        async for result in self._steam_price(
            event, auto_first=False, prefix="price", query_override=reply
        ):
            yield result

    async def _price_ack(self, event, text: str):
        """查询进度提示：直接走 context.send_message，不占用 handler 的 yield 次数。

        部分适配器/装饰插件在第一次 yield 后会截断 async generator，
        导致后续真正查价逻辑没机会执行，表现为机器人卡死。
        """
        try:
            umo = getattr(event, "unified_msg_origin", None) or None
            if umo is None:
                getter = getattr(event, "get_unified_msg_origin", None)
                umo = getter() if callable(getter) else None
            if umo:
                await self.context.send_message(umo, MessageChain().message(text))
                return None
        except Exception as e:
            logger.warning(f"[查价] 进度提示发送失败: {e}")
        return text

    def _price_cache_ttl(self) -> float:
        """查价候选序号缓存有效期（秒）。默认 3 分钟，过期后需重新 /price。"""
        try:
            v = float((self.config or {}).get("price_search_cache_sec", 180) or 0)
        except Exception:
            v = 180.0
        return v if v > 0 else 180.0

    def _price_cache_age(self, session_key):
        if not hasattr(self, "_steam_search_ts"):
            self._steam_search_ts = {}
        ts = self._steam_search_ts.get(session_key)
        if ts is None:
            return None
        return time.time() - float(ts)

    def _clear_price_search_cache(self, session_key):
        self._steam_search_pending.pop(session_key, None)
        self._steam_search_cache.pop(session_key, None)
        if not hasattr(self, "_steam_search_ts"):
            self._steam_search_ts = {}
        self._steam_search_ts.pop(session_key, None)

    def _set_price_search_cache(self, session_key, games):
        self._steam_search_cache[session_key] = list(games)
        self._steam_search_pending[session_key] = True
        if not hasattr(self, "_steam_search_ts"):
            self._steam_search_ts = {}
        self._steam_search_ts[session_key] = time.time()

    def _expire_price_search_cache(self, session_key) -> bool:
        """缓存过期则清理并返回 True。"""
        if not hasattr(self, "_steam_search_ts"):
            self._steam_search_ts = {}
        pending = self._steam_search_pending.get(session_key)
        ts = self._steam_search_ts.get(session_key)
        if not pending:
            return False
        # 无时间戳：视为过期（避免旧进程内存一直挂着）
        if ts is None:
            self._clear_price_search_cache(session_key)
            return True
        age = time.time() - float(ts)
        ttl = self._price_cache_ttl()
        if age > ttl:
            logger.info(
                f"[查价] 候选缓存过期 age={age:.0f}s ttl={ttl:.0f}s "
                f"({int(age/60)}min>{int(ttl/60)}min) key={session_key}"
            )
            self._clear_price_search_cache(session_key)
            return True
        return False

    @staticmethod
    def _contains_chinese(text: str) -> bool:
        return any("\u4e00" <= char <= "\u9fff" for char in text)

    async def _translate_game_query(self, query: str) -> str:
        """将中文游戏名转换为 Steam/ITAD 更容易命中的英文官方名。
        加锁串行 + 结果缓存：并发查询不排队挤爆 LLM，重复查询直接读缓存。"""
        if not self._contains_chinese(query):
            return query
        cached = self._translate_cache.get(query)
        if cached:
            return cached
        try:
            async with self._translate_lock:
                # 双检查：等待锁期间可能已被其他查询翻译
                cached = self._translate_cache.get(query)
                if cached:
                    return cached
                provider = self.context.get_using_provider()
                if not provider:
                    return query
                # 超时保护：LLM 挂起时 12 秒快速回退原始查询，避免指令卡死
                response = await asyncio.wait_for(
                    provider.text_chat(
                        prompt=(
                            "请将以下游戏名翻译为 Steam 商店使用的英文官方名称，"
                            f"仅输出英文名，不要输出其他内容：{query}"
                        ),
                        contexts=[],
                        image_urls=[],
                        func_tool=None,
                        system_prompt="",
                    ),
                    timeout=12,
                )
                raw = str(response.completion_text or "").strip()
            # 剥离 LLM 推理/思考内容：取最后一个 </thinking>/</think> 之后的部分作为最终答案，
            # 前面的思考内容丢弃；若没有闭合标签则删除独立 think 标签，避免思考内容混入搜索结果。
            closes = list(re.finditer(r"</(?:thinking|think)[^>]*>", raw, flags=re.I))
            if closes:
                raw = raw[closes[-1].end():]
            else:
                raw = re.sub(r"<think[^>]*>", "", raw, flags=re.I)
                raw = re.sub(r"</?(?:thinking|think)[^>]*>", "", raw, flags=re.I)
            translated = re.sub(
                r"^(?:英文名|翻译结果|Translation)\s*[:：]?\s*",
                "",
                raw,
                flags=re.IGNORECASE,
            ).strip().strip('`\"“”')
            if translated:
                self._translate_cache[query] = translated
                logger.info("[LLM][翻译游戏名] %s -> %s", query, translated)
                return translated
        except asyncio.TimeoutError:
            logger.warning("LLM 翻译游戏名超时（12s），尝试中转站: %s", query)
            return query
        except Exception as exc:
            logger.warning("LLM 翻译游戏名失败，尝试中转站: %s", exc)
            return query

    async def _format_game_search_item(self, game):
        """返回候选展示名 + DLC/版本标签。

        注意：Steam storesearch 对 DLC 也返回 type=app，不可信。
        标记必须以 appdetails 的 type（game/dlc/...）为准。
        """
        title = str(getattr(game, "title", None) or "").strip() or "未知游戏"
        zh = str(getattr(game, "title_zh", None) or "").strip()
        ctype = str(getattr(game, "content_type", None) or "").lower().strip()
        # GetItems: 0/已归一 game、4/已归一 dlc 均可信
        reliable = ctype in {
            "game", "dlc", "music", "episode", "mod", "demo", "video", "hardware", "advertising",
            "0", "4",
        }
        if game.appid and (not zh or not reliable):
            cache = getattr(self, "_app_meta_cache", None)
            if cache is None:
                cache = {}
                self._app_meta_cache = cache
            key = str(game.appid)
            meta = cache.get(key)
            if meta is None:
                try:
                    meta = await self.fetch_app_meta(game.appid) or {}
                except Exception as e:
                    logger.warning(f"[查价] app meta 失败 appid={key}: {e}")
                    meta = {}
                cache[key] = meta
            if meta:
                if not zh:
                    zh = str(meta.get("name") or "").strip()
                mtype = str(meta.get("type") or "").lower().strip()
                if mtype:
                    ctype = mtype
            if not zh:
                try:
                    zh = str(await self.get_chinese_game_name(game.appid, "") or "").strip()
                except Exception:
                    zh = ""
            try:
                game.content_type = ctype
                game.title_zh = zh
            except Exception:
                pass

        display = title
        if zh and zh != title and ((not self._has_cjk_text(title)) or (zh not in title)):
            display = f"{title}（{zh}）"
        # 发行商标注：云豹 / GungHo 美版 等
        pubs = list(getattr(game, "publishers", None) or [])
        if not pubs and game.appid:
            cache = getattr(self, "_app_meta_cache", None) or {}
            info = cache.get(str(game.appid)) or {}
            pubs = list(info.get("publishers") or [])
        pub_tag = ""
        pub_l = " ".join(pubs).lower()
        if "clouded" in pub_l or "云豹" in pub_l:
            pub_tag = "云豹"
        elif "gungho" in pub_l or "america" in pub_l:
            pub_tag = "美版"
        if pub_tag and pub_tag not in display:
            display = f"{display} [{pub_tag}]"
        try:
            game.publisher_tag = pub_tag
        except Exception:
            pass

        tags = []
        itad_client = getattr(self, "ITAD_CLIENT", None)
        looks_dlc = getattr(itad_client, "_looks_like_dlc", None) if itad_client else None
        looks_edition = getattr(itad_client, "_looks_like_edition", None) if itad_client else None
        names = [n for n in (title, zh) if n]
        # GetItems type=4 与 appdetails type=dlc 同等
        type_is_dlc = ctype in {"dlc", "dlc_detail", "music", "episode", "mod", "demo", "4"}
        type_is_game = ctype in {"game", "0"}
        type_is_package = ctype in {"sub", "package", "bundle"}
        name_is_dlc = callable(looks_dlc) and any(looks_dlc(n) for n in names)
        name_is_edition = callable(looks_edition) and any(looks_edition(n) for n in names)
        if type_is_dlc or (name_is_dlc and not type_is_game and not type_is_package):
            tags.append("DLC")
        elif type_is_package or name_is_edition:
            if not type_is_dlc:
                tags.append("版本包")
        return display, tags

    def _persist_price_to_local_store(self, game, region_prices: dict):
        """查价后写入本地库：games + prices + price_history（观测史低）。"""
        import time as _time
        from datetime import date as _date
        conn = self._store_conn()
        if conn is None:
            return
        aid = str(getattr(game, "appid", "") or "").strip()
        if not aid.isdigit():
            return
        title = str(getattr(game, "title", "") or "").strip()
        zh = str(getattr(game, "title_zh", "") or "").strip()
        ctype = str(getattr(game, "content_type", "") or "").lower()
        try:
            lstore.upsert_game(
                conn,
                appid=aid,
                name=zh or title,
                content_type=ctype or "game",
            )
        except Exception as e:
            logger.debug(f"[查价] upsert_game {aid}: {e}")
        today = _date.today().isoformat()
        now = _time.time()
        for label, info in (region_prices or {}).items():
            info = info or {}
            cur = info.get("current_price")
            if cur is None:
                continue
            try:
                lstore.upsert_price(
                    conn,
                    aid,
                    str(label),
                    currency=str(info.get("currency") or "CNY"),
                    current_price=float(cur),
                    regular_price=info.get("regular"),
                    cut=info.get("cut"),
                    lowest=info.get("lowest"),
                    lowest_currency=info.get("lowest_currency") or info.get("lowest_cur") or info.get("currency"),
                    lowest_note=info.get("lowest_note") or "",
                    observed_at=now,
                    source="price_query",
                )
            except Exception as e:
                logger.debug(f"[查价] upsert_price {aid}/{label}: {e}")
            # 观测价：若低于库中史低则更新（set_price_history 本身只保留更低）
            try:
                low_val = info.get("lowest")
                if low_val is None:
                    low_val = cur
                lstore.set_price_history(
                    conn,
                    aid,
                    str(label),
                    float(low_val),
                    str(info.get("currency") or "CNY"),
                    today,
                )
                # 同时用当前价做一次观测，便于识别「新史低」
                lstore.set_price_history(
                    conn,
                    aid,
                    f"{label}|obs",
                    float(cur),
                    str(info.get("currency") or "CNY"),
                    today,
                )
            except Exception as e:
                logger.debug(f"[查价] set_price_history {aid}/{label}: {e}")
        try:
            conn.commit()
        except Exception:
            pass

    @staticmethod
    def _clip_dlc_name(name: str, width: int = 28) -> str:
        s = str(name or "").strip()
        if len(s) <= width:
            return s
        return s[: width - 1] + "…"

    def _local_dlc_summary_lines(self, *, game, parent_appid, is_package, title_tags, compact=False):
        """本地库 DLC 摘要文案。

        - 本体：只报总数 + 最多 3 个示例，不全量挂 DLC
        - DLC：补一行所属本体
        - 版本包：交给下面的「版本套餐」块，此处不重复
        名称尽量写全：≤3 个全列；更多时示例放宽到 28 字，过长则一行一个。
        """
        out = []
        if is_package:
            return out
        conn = self._store_conn()
        if conn is None:
            return out
        aid = str(getattr(game, "appid", "") or "").strip()
        if not aid.isdigit():
            return out
        tags = [str(t) for t in (title_tags or [])]
        ctype = str(getattr(game, "content_type", "") or "").lower()
        is_dlc_tag = "DLC" in tags or ctype in {"dlc", "dlc_detail", "4", "music", "episode", "mod", "demo"}

        # DLC → 本体
        if is_dlc_tag:
            parent = str(parent_appid or "").strip()
            if not parent or not parent.isdigit():
                parent = lstore.get_parent_of_app(conn, aid)
            if parent and parent.isdigit() and parent != aid:
                row = lstore.get_game(conn, parent) or {}
                pname = str(row.get("name") or row.get("name_zh") or "").strip() or parent
                out.append("")
                out.append(f"🧩 所属本体：《{self._clip_dlc_name(pname, 40)}》（appid {parent}）")
            return out

        # 本体 → DLC 数量摘要（限量示例，名称尽量写全）
        base_aid = aid
        total, samples = lstore.list_dlc_for_parent(conn, base_aid, sample=3)
        if total <= 0:
            return out
        # 示例名去掉与本体重复的长前缀，避免全变成「Train Simulator…」
        base_row = lstore.get_game(conn, base_aid) or {}
        base_name = str(base_row.get("name") or "").strip()
        cleaned = []
        for n in samples:
            s = str(n or "").strip()
            if base_name and len(base_name) >= 4 and s.casefold().startswith(base_name.casefold()):
                s2 = s[len(base_name):].lstrip(" :-–—|")
                if s2:
                    s = s2
            cleaned.append(s)
        # 示例若仍共享长公共前缀（如 Rocksmith® 2014 – …），再砍一刀
        if len(cleaned) >= 2:
            cp = cleaned[0]
            for n in cleaned[1:]:
                while cp and not n.casefold().startswith(cp.casefold()):
                    cp = cp[:-1]
            if len(cp) >= 8:
                cut = cp
                for sep in (" – ", " - ", "—", "–", " ", ":", "："):
                    idx = cp.rfind(sep)
                    if idx >= 6:
                        cut = cp[:idx]
                        break
                if len(cut) >= 6:
                    cleaned = [
                        n[len(cut):].lstrip(" :-–—|") if n.casefold().startswith(cut.casefold()) else n
                        for n in cleaned
                    ]
        out.append("")
        if total <= 3:
            # 少量 DLC：名称写全，不截断
            shown = [str(n).strip() for n in cleaned if str(n).strip()]
            if not shown:
                shown = [str(n).strip() for n in samples if str(n).strip()]
            if shown:
                out.append(f"🧩 资料库 DLC {total} 个：{'、'.join(shown)}")
            else:
                out.append(f"🧩 资料库 DLC {total} 个")
            return out
        # 多个 DLC：3 个示例，名称放宽；仍超长则一行一个
        raw_shown = [str(n).strip() for n in cleaned if str(n).strip()]
        if not raw_shown:
            raw_shown = [str(n).strip() for n in samples if str(n).strip()]
        need_multiline = any(len(n) > 28 for n in raw_shown) or sum(len(n) for n in raw_shown) > 48
        if need_multiline and raw_shown:
            out.append(f"🧩 资料库 DLC 共 {total} 个，示例（{len(raw_shown)}/{total}）：")
            for n in raw_shown:
                out.append(f"· {self._clip_dlc_name(n, 36)}")
            out.append("（不全列，可搜具体 DLC 名）")
        else:
            shown = [self._clip_dlc_name(n, 28) for n in raw_shown]
            joined = "、".join(shown) if shown else "名称未入库"
            out.append(f"🧩 资料库 DLC 共 {total} 个，示例：{joined}…（不全列，可搜具体 DLC 名）")
        return out

    @staticmethod
    def _parse_price_indices(query: str) -> list:
        """解析候选序号：支持 '1' / '1 2' / '1,2' / '1、2'。"""
        import re as _re
        raw = str(query or "").strip()
        if not raw:
            return []
        parts = [p for p in _re.split(r"[\s,，、/]+", raw) if p]
        if not parts or not all(p.isdigit() for p in parts):
            return []
        nums = []
        for p in parts:
            n = int(p)
            if n not in nums:
                nums.append(n)
        return nums

    async def _safe_call(self, coro, timeout=8.0, default=None, label=""):
        """带超时的异步调用，防止单个接口挂起拖死整次查询。"""
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning(f"[查价] {label} 超时({timeout}s)")
            return default
        except Exception as e:
            logger.warning(f"[查价] {label} 失败: {e}")
            return default

    async def _collect_price_for_game(self, game, *, compact: bool = False) -> dict:
        """查询单款游戏价格，返回 {image, msg, title}；不直接发送。

        连查时按完整单查逻辑逐款执行，结果先入临时缓存，最后由调用方拼成一条消息。
        compact=True 时只查主区+套餐（备用，连查默认不用）。
        """
        logger.info(f"[查价] 开始 game={getattr(game,'title',None)!r} appid={getattr(game,'appid',None)} compact={compact}")
        try:
            await self._ensure_game_itad_id(game)
        except Exception as e:
            logger.warning(f"[查价] 补全 appid/ITAD id 失败: {e}")
        # 本地库补中文名/appid（ITAD 英文标题时仍显示《仁王３》等）
        try:
            _conn_pre = self._store_conn()
            _aid_pre = str(getattr(game, "appid", "") or "").strip()
            if _conn_pre is not None:
                _row_pre = None
                if _aid_pre.isdigit():
                    _row_pre = lstore.get_game(_conn_pre, _aid_pre)
                if not _row_pre:
                    # 用 ITAD slug/id 反查本地 itad_id 或名称
                    _title_pre = str(getattr(game, "title", "") or "")
                    if _title_pre:
                        try:
                            _r2 = _conn_pre.execute(
                                "SELECT appid,name,content_type FROM games WHERE name=? OR name_zh=? LIMIT 1",
                                (_title_pre, _title_pre),
                            ).fetchone()
                            if _r2:
                                _row_pre = dict(_r2) if not isinstance(_r2, dict) else _r2
                        except Exception:
                            _row_pre = None
                if _row_pre:
                    _ln = str((_row_pre.get("name") if isinstance(_row_pre, dict) else _row_pre["name"]) or "").strip()
                    _la = str((_row_pre.get("appid") if isinstance(_row_pre, dict) else _row_pre["appid"]) or "").strip()
                    if _la.isdigit() and not str(getattr(game, "appid", "") or "").isdigit():
                        try:
                            game.appid = _la
                        except Exception:
                            pass
                    if _ln and _ln != getattr(game, "title", None):
                        try:
                            # 中文名优先展示
                            if any("一" <= ch <= "鿿" for ch in _ln):
                                game.title_zh = _ln
                                if not any("一" <= ch <= "鿿" for ch in str(getattr(game, "title", "") or "")):
                                    game.title = _ln
                        except Exception:
                            pass
        except Exception as e:
            logger.debug(f"[查价] 本地名/appid 补全失败: {e}")
        if not str(getattr(game, "appid", "") or "").isdigit():
            logger.warning(f"[查价] 仍无 appid，区域价可能为空 game={getattr(game,'title',None)!r}")
        price_region = (self.config.get("price_region", "CN") or "CN").strip().upper() or "CN"
        compare_region_raw = (self.config.get("price_compare_regions", "IN,UA,PK") or "NONE").strip()
        compare_regions = [
            r.strip().upper() for r in compare_region_raw.split(",")
            if r.strip().upper() and r.strip().upper() != "NONE"
        ]
        if compact:
            region_codes = [price_region]
        else:
            region_codes = [price_region] + [r for r in compare_regions if r != price_region]
        # 进程内短缓存：同一 game+region 在本条命令里只打一次接口
        cache = getattr(self, "_price_call_cache", None)
        if cache is None:
            cache = {}
            self._price_call_cache = cache

        async def _itad_summary(gid, country):
            gid = str(gid or "").strip()
            country = str(country or "CN").upper()
            # ITAD 价格接口要 UUID，不能传 steam:appid
            if gid.startswith("steam:"):
                gid = ""
            key = ("itad", gid or f"appid:{getattr(game,'appid','')}", country)
            if key in cache:
                return cache[key]
            val = None
            if gid:
                val = await self._safe_call(
                    self.ITAD_CLIENT.get_price_summary(gid, country),
                    timeout=10.0, default=None, label=f"ITAD {gid}/{country}",
                )
            # game.id 缺失/无效（steam: 前缀）时，用 Steam appid 反查 ITAD
            need_map = (
                not val
                or not (val.get("history_low") is not None or val.get("steam_store_low") is not None or val.get("steam_history_low") is not None)
            )
            appid = str(getattr(game, "appid", "") or "").strip()
            if not appid and str(getattr(game, "id", "") or "").startswith("steam:"):
                appid = str(game.id).split(":", 1)[-1]
                try:
                    game.appid = appid
                except Exception:
                    pass
            if need_map and appid and appid.isdigit():
                aid_key = ("itad_app", appid, country)
                aid_val = cache.get(aid_key)
                if aid_val is None:
                    mapped = await self._safe_call(
                        self.ITAD_CLIENT.lookup_by_appid(appid),
                        timeout=8.0, default={}, label=f"ITAD lookup {appid}",
                    ) or {}
                    aid_val = str(mapped.get("id") or "")
                    cache[aid_key] = aid_val
                if aid_val:
                    val2 = await self._safe_call(
                        self.ITAD_CLIENT.get_price_summary(aid_val, country),
                        timeout=10.0, default=None, label=f"ITAD {aid_val}/{country}",
                    )
                    if val2:
                        val = val2
                        try:
                            game.id = aid_val
                        except Exception:
                            pass
            cache[key] = val
            return val

        async def _store_price(appid, country):
            key = ("store", str(appid), str(country).upper())
            if key in cache:
                return cache[key]
            if steam_store_blocked():
                cache[key] = None
                return None
            val = await self._safe_call(
                self.fetch_region_price(appid, country),
                timeout=8.0, default=None, label=f"store {appid}/{country}",
            )
            cache[key] = val
            return val

        from ...shared.utils.price import to_cny
        region_prices = {}
        REGION_CN = {
            "CN": "国区", "UA": "乌克兰", "RU": "俄罗斯", "IN": "印度",
            "PK": "南亚（巴基斯坦）", "US": "美国", "JP": "日本", "KR": "韩国",
            "TR": "土耳其", "HK": "香港", "SG": "新加坡", "TH": "泰国",
            "VN": "越南", "MY": "马来西亚", "ID": "印尼", "PH": "菲律宾",
        }
        if game.appid:
            conn_l = self._store_conn()
            price_ttl = float((self.config or {}).get("local_price_ttl_sec", 7200) or 7200)
            _obs_hist = {}
            if conn_l is not None:
                try:
                    _obs_hist = lstore.get_price_history_map(conn_l, str(game.appid)) or {}
                except Exception:
                    _obs_hist = {}
            if not _obs_hist:
                try:
                    import os as _os
                    _hist_path = _os.path.join(self.data_dir, "price_history.json")
                    if _os.path.exists(_hist_path):
                        with open(_hist_path, encoding="utf-8") as f:
                            _all = json.load(f) or {}
                        _obs_hist = _all.get(str(game.appid)) or {}
                except Exception:
                    _obs_hist = {}
            obs_app = _obs_hist or {}
            local_rows = {}
            if conn_l is not None:
                try:
                    for r in lstore.get_prices_for_appid(conn_l, str(game.appid)):
                        if price_ttl and time.time() - float(r.get("observed_at") or 0) > price_ttl:
                            continue
                        local_rows[str(r.get("region") or "")] = r
                except Exception:
                    local_rows = {}

            for region in region_codes:
                label_cn = REGION_CN.get(region, region)
                itad = None
                lr = local_rows.get(label_cn)
                if lr and lr.get("current_price") is not None:
                    store_price = {
                        "currency": lr.get("currency"),
                        "current_price": lr.get("current_price"),
                        "current_regular": lr.get("regular_price"),
                        "cut": lr.get("cut"),
                    }
                else:
                    store_price = await _store_price(game.appid, region)
                    itad = await _itad_summary(getattr(game, "id", "") or "", region)
                    # ITAD 对乌克兰/巴基斯坦/俄罗斯无本地覆盖，会回退返回美国区 USD，
                    # 与当地币种不可比（实测出现"当前 524 UAH / 史低 2.99 USD"）→ 抑制史低，显示暂无
                    if str(region).upper() in ("UA", "PK", "RU"):
                        itad = None
                    if not compact:
                        await asyncio.sleep(0.8)
                cur = (store_price or {}).get("currency") or (itad or {}).get("currency")
                current = (store_price or {}).get("current_price")
                if current is None and itad and itad.get("current_price") is not None:
                    store_price = {
                        "currency": itad.get("currency"),
                        "current_price": itad.get("current_price"),
                        "current_regular": itad.get("current_regular"),
                        "cut": itad.get("cut"),
                    }
                    cur = itad.get("currency")
                    current = itad.get("current_price")
                if current is None or not cur:
                    continue
                cur = str(cur).upper()
                low_amt = None
                low_cur = cur
                low_note = ""
                steam_hist = (itad or {}).get("steam_history_low")
                steam_store = (itad or {}).get("steam_store_low")
                if steam_hist is not None:
                    try:
                        low_amt = float(steam_hist)
                        low_cur = (itad or {}).get("steam_history_low_currency") or cur
                    except (TypeError, ValueError):
                        low_amt = None
                if low_amt is None and steam_store is not None:
                    try:
                        low_amt = float(steam_store)
                        low_cur = (itad or {}).get("steam_store_low_currency") or cur
                    except (TypeError, ValueError):
                        low_amt = None
                if low_amt is None and lr and lr.get("lowest") is not None:
                    try:
                        low_amt = float(lr.get("lowest"))
                        low_cur = str(lr.get("lowest_currency") or cur)
                        low_note = str(lr.get("lowest_note") or "")
                    except (TypeError, ValueError):
                        low_amt = None
                if low_amt is None:
                    rec = obs_app.get(label_cn) or {}
                    try:
                        if rec.get("price") is not None:
                            low_amt = float(rec["price"])
                            low_cur = str(rec.get("currency") or cur).upper()
                            low_note = "（观测）"
                    except (TypeError, ValueError):
                        low_amt = None
                region_prices[label_cn] = {
                    "currency": cur,
                    "current_price": current,
                    "current_cny": to_cny(current, cur),
                    "regular": (store_price or {}).get("current_regular"),
                    "cut": (store_price or {}).get("cut"),
                    "lowest": low_amt,
                    "lowest_currency": low_cur,
                    "lowest_cny": to_cny(low_amt, low_cur) if low_amt is not None else None,
                    "lowest_note": low_note,
                }
                if conn_l is not None:
                    try:
                        lstore.upsert_price(
                            conn_l,
                            str(game.appid),
                            label_cn,
                            currency=cur,
                            current_price=current,
                            regular_price=(store_price or {}).get("current_regular"),
                            cut=(store_price or {}).get("cut"),
                            lowest=low_amt,
                            lowest_currency=low_cur,
                            lowest_note=low_note,
                            source="collect_price",
                        )
                        lstore.upsert_game(
                            conn_l,
                            appid=str(game.appid),
                            name=str(getattr(game, "title_zh") or getattr(game, "title", "") or "") or None,
                            itad_id=str(getattr(game, "id", "") or "") or None,
                        )
                        # 史低/观测写入 price_history（更低才覆盖）
                        try:
                            from datetime import date as _dt
                            _today = _dt.today().isoformat()
                            if current is not None:
                                lstore.set_price_history(
                                    conn_l, str(game.appid), label_cn,
                                    float(current), cur, _today,
                                )
                            if low_amt is not None:
                                lstore.set_price_history(
                                    conn_l, str(game.appid), label_cn,
                                    float(low_amt), str(low_cur or cur), _today,
                                )
                        except Exception as _he:
                            logger.debug(f"[local_store] price_history fail: {_he}")
                        conn_l.commit()
                    except Exception as e:
                        logger.debug(f"[local_store] price save fail: {e}")
        detail = None
        pkg_info = None
        parent_appid = ""
        if game.appid:
            ctype = str(getattr(game, "content_type", "") or "").lower()
            # 捆绑包/sub（数字豪华版等）：appdetails 查不到价，走 packagedetails
            if ctype in {"sub", "package", "bundle"} or not region_prices:
                pkg_info = await self._safe_call(
                    self.fetch_package_details(game.appid, price_region),
                    timeout=10.0, default=None, label=f"pkg {game.appid}",
                )
            detail = await self._safe_call(
                self.fetch_game_details(game.appid), timeout=10.0, default=None, label=f"detail {game.appid}"
            )
            if detail and game.appid and not compact:
                reviews = await self._safe_call(
                    self.fetch_game_reviews_both(game.appid), timeout=8.0, default=None, label=f"reviews {game.appid}"
                )
                if reviews:
                    detail['review_all'] = reviews.get('all') or {}
                    detail['review_schinese'] = reviews.get('schinese') or {}

        is_package = bool(pkg_info)
        if is_package:
            try:
                game.content_type = "sub"
            except Exception:
                pass
            apps = (pkg_info or {}).get("apps") or []
            if apps:
                parent_appid = str(apps[0].get("id") or "")
            if not detail and parent_appid:
                detail = await self._safe_call(
                    self.fetch_game_details(parent_appid), timeout=10.0, default=None,
                    label=f"detail parent {parent_appid}",
                )
            store_url = f"https://store.steampowered.com/sub/{game.appid}/"
            store_appid = game.appid
        else:
            store_appid = (detail or {}).get('store_appid') or game.appid
            store_url = f"https://store.steampowered.com/app/{store_appid}/" if store_appid else ""
        logger.info(f"[查价] 区域价完成 game={getattr(game,'title',None)!r} regions={list(region_prices.keys())} package={is_package} parent={parent_appid} compact={compact}")

        # ---- 自建观测史低（不依赖 ITAD 的兜底：本地存档，标注历史最低观测值） ----
        try:
            import os as _os
            _hist_path = _os.path.join(self.data_dir, "price_history.json")
            _hist = {}
            if _os.path.exists(_hist_path):
                with open(_hist_path, encoding="utf-8") as f:
                    _hist = json.load(f) or {}
            _hist.setdefault(str(store_appid or game.appid), {})
            today = date.today().isoformat()
            for label, info in region_prices.items():
                cur = info.get("currency") or "CNY"
                price = info.get("current_price")
                if price is None:
                    continue
                key = str(label)
                _rec = _hist[str(store_appid or game.appid)].setdefault(key, {})
                prev_low = None
                if _rec.get("price") is not None:
                    try:
                        prev_low = float(_rec["price"])
                    except (TypeError, ValueError):
                        prev_low = None
                if prev_low is None or price < prev_low:
                    _rec.update({"price": price, "currency": cur, "date": today})
                    try:
                        conn_h = self._store_conn()
                        if conn_h is not None:
                            lstore.set_price_history(
                                conn_h,
                                str(store_appid or game.appid),
                                str(label),
                                price,
                                str(cur or ""),
                                today,
                            )
                            conn_h.commit()
                    except Exception:
                        pass
            try:
                with open(_hist_path, "w", encoding="utf-8") as f:
                    json.dump(_hist, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        except Exception as e:
            logger.warning(f"[查价] 观测史低存档失败: {e}")

        # ---- 文字模式输出：封面图 + 文本（快，不渲染卡片） ----
        from ...presentation.renderers.game_detail import CURRENCY_SYMBOL

        def _val(v, cur, dollar=True):
            if v is None:
                return ""
            sym = CURRENCY_SYMBOL.get(str(cur).upper(), "$" if dollar else "")
            return f"{sym}{v:g}"

        # 标题带上 DLC / 版本包标记
        _title_display, _title_tags = await self._format_game_search_item(game)
        title_line = f"🎮《{game.title}》"
        if is_package and "版本包" not in _title_tags:
            _title_tags = list(_title_tags) + ["版本包"]
        if parent_appid and is_package:
            title_line = f"🎮《{game.title}》 [ 版本包 ]（所属本体 appid {parent_appid}）"
        elif _title_tags:
            title_line = f"🎮《{game.title}》 [ {' / '.join(_title_tags)} ]"
            if _title_display and "（" in _title_display:
                zh_only = _title_display.split("（", 1)[-1].rstrip("）")
                if zh_only and zh_only != game.title:
                    title_line = f"🎮《{game.title}》（{zh_only}）[ {' / '.join(_title_tags)} ]"
        lines = [title_line]
        if is_package:
            lines.append("💡 此条目为 Steam 捆绑包/版本套餐，不是独立游戏本体。")
            lines.append("   价格来自 packagedetails；查本体后可在「版本套餐」中对比豪华版。")
        if steam_store_blocked() and not region_prices:
            try:
                lines.append(f"⚠ {steam_store_guard_msg() or '商店接口冷却中，本次价格可能来自 ITAD/本地缓存。'}")
            except Exception:
                lines.append("⚠ Steam 商店接口冷却中，本次价格可能来自 ITAD/本地缓存。")
        # 区域价格（当地货币 + ¥换算 + 史低）；配置了但查不到价的区明确标注锁区/未提供
        shown = set()
        for label, info in region_prices.items():
            shown.add(label)
            info = info or {}
            cur = info.get("currency") or "CNY"
            part = _val(info.get("current_price"), cur)
            if info.get("current_cny") is not None and str(cur).upper() != "CNY":
                part += f"（¥{info['current_cny']:.2f}）"
            if info.get("cut"):
                part += f" -{int(info['cut'])}%"
            if info.get("lowest") is None:
                part += "｜Steam史低 暂无"
            else:
                low = _val(info["lowest"], info.get("lowest_currency") or cur)
                if info.get("lowest_cny") is not None and str(info.get("lowest_currency") or cur).upper() != "CNY":
                    low += f"（¥{info['lowest_cny']:.2f}）"
                low += str(info.get("lowest_note") or "")
                # 保护：Steam史低不得高于当前价
                try:
                    if float(info["lowest"]) > float(info["current_price"]):
                        info["lowest"] = info["current_price"]
                        info["lowest_currency"] = cur
                        info["lowest_cny"] = info.get("current_cny")
                        low = _val(info["lowest"], cur)
                        if str(cur).upper() != "CNY":
                            cny_low = info.get("current_cny")
                            if cny_low is not None:
                                low += f"（¥{cny_low:.2f}）"
                        low += "（按当前价修正）"
                except (TypeError, ValueError):
                    pass
                part += f"｜Steam史低 {low}"
            lines.append(f"· {label}：{part}")
        # 补齐配置了但查不到价的区（锁区/未发售/接口失败时明确标注）
        for region in region_codes:
            label = REGION_CN.get(region, region)
            if label not in shown:
                lines.append(f"· {label}：未提供（锁区/暂未发售，或数据源无该区记录）")

        # 商店页折扣截止（仅当前有折扣且商店未进全局冷却）
        sale_end_ts = None
        sale_end_text = None
        sale_end_line = ""
        try:
            max_cut = 0
            for _info in (region_prices or {}).values():
                try:
                    max_cut = max(max_cut, int((_info or {}).get("cut") or 0))
                except (TypeError, ValueError):
                    continue
            sale_appid = str(store_appid or parent_appid or game.appid or "").strip()
            if sale_appid and sale_appid.isdigit() and max_cut > 0 and not steam_store_blocked():
                _end = await self.fetch_store_sale_end(sale_appid) or {}
                sale_end_ts = _end.get("end_ts")
                sale_end_text = _end.get("end_text")
                sale_end_line = format_sale_end_line(sale_end_ts, sale_end_text)
            elif sale_appid and max_cut > 0 and steam_store_blocked():
                sale_end_line = "（商店限流中，本次未取截止时间）"
            if sale_end_line:
                lines.append(f"· 折扣时限：{sale_end_line}")
        except Exception as se_err:
            logger.debug(f"[查价] 折扣时限获取失败: {se_err}")

        # ---- 本地库：写入游戏元数据 + 当前价/史低（供以后判断新史低）----
        try:
            self._persist_price_to_local_store(game, region_prices)
        except Exception as e:
            logger.debug(f"[查价] 写入本地价库失败: {e}")

        # ---- 本地库 DLC 摘要（限量示例，禁止全量挂出）----
        try:
            lines.extend(self._local_dlc_summary_lines(
                game=game,
                parent_appid=parent_appid,
                is_package=is_package,
                title_tags=_title_tags,
                compact=compact,
            ))
        except Exception as e:
            logger.debug(f"[查价] 本地 DLC 摘要失败: {e}")

        # 版本套餐价：豪华版/终极版等（Steam package_groups）— 多区合并
        # 捆绑包条目改查所属本体的套餐表
        editions = []
        ed_appid = parent_appid if (is_package and parent_appid) else game.appid
        if ed_appid:
            ed_regions = [price_region] if compact else list(region_codes)
            seen_cc = set()
            ed_regions = [c for c in ed_regions if not (c in seen_cc or seen_cc.add(c))]
            editions = await self._safe_call(
                self.fetch_edition_prices_multi(ed_appid, ed_regions),
                timeout=12.0, default=[], label=f"editions-multi {ed_appid}",
            ) or []
            if is_package and parent_appid:
                lines.append("")
                lines.append(f"📦 本体《{(detail or {}).get('name') or parent_appid}》的版本套餐：")
        if editions:
            lines.append("")
            lines.append("🛒 版本套餐")
            shown_ed = 0
            for ed in editions:
                if shown_ed >= 6:
                    break
                name = ed.get("name") or "套餐"
                lines.append(f"· {name}")
                reg_map = ed.get("regions") or {}
                row_parts = []
                for cc in (ed_regions or []):
                    info = reg_map.get(cc) or {}
                    if info.get("price") is None:
                        continue
                    label = REGION_CN.get(cc, cc)
                    e_cur = info.get("currency") or cc
                    e_part = _val(info.get("price"), e_cur)
                    # 非人民币补 ¥ 换算
                    if str(e_cur).upper() != "CNY":
                        e_cny = to_cny(info.get("price"), e_cur)
                        if e_cny is not None:
                            e_part += f"（¥{e_cny:.2f}）"
                    if info.get("cut"):
                        e_part += f" -{int(info['cut'])}%"
                    row_parts.append(f"{label} {e_part}")
                if row_parts:
                    lines.append("  " + "｜".join(row_parts))
                else:
                    # 兜底：任意区有价就展示
                    for cc, info in reg_map.items():
                        if info.get("price") is None:
                            continue
                        e_cur = info.get("currency") or cc
                        e_part = _val(info.get("price"), e_cur)
                        if str(e_cur).upper() != "CNY":
                            e_cny = to_cny(info.get("price"), e_cur)
                            if e_cny is not None:
                                e_part += f"（¥{e_cny:.2f}）"
                        lines.append(f"  {REGION_CN.get(cc, cc)} {e_part}")
                shown_ed += 1

        # 简介
        desc = (detail or {}).get("short_description") or ""
        if desc:
            desc = desc.strip()[:180]
            lines.append(f"\n📖 {desc}")
        # HowLongToBeat 通关时长（可选；走 ITAD 页里的 HLTB 链接，失败不显示）
        try:
            from ...infrastructure.clients.hltb import HLTBClient
            hltb = getattr(self, "_hltb_client", None)
            if hltb is None:
                hltb = HLTBClient(data_dir=self.data_dir, proxy=self.proxy)
                self._hltb_client = hltb
            # 搜索结果里的 ITAD slug（来自 search_games）
            slug = str(getattr(game, "slug", "") or "")
            itad_id = str(getattr(game, "id", "") or "")
            hltb_title = str(getattr(game, "title_zh", "") or game.title or "")
            hltb_data = await self._safe_call(
                hltb.lookup(hltb_title, itad_slug=slug, itad_game_id=itad_id),
                timeout=12.0, default=None, label="hltb",
            )
            hltb_line = hltb.format_line(hltb_data) if hltb_data else ""
            if hltb_line:
                lines.append("")
                lines.append(hltb_line)
        except Exception as e:
            logger.warning(f"[查价] HLTB 时长附加失败（忽略）: {e}")
        if store_url:
            lines.append(f"\n🔗 {store_url}")
        msg = "\n".join(lines)
        # 封面必须与查价 appid 一致，禁止 ITAD 图混入（会串成美版/别的作）
        image = ""
        header = (detail or {}).get("header_image") or ""
        if header:
            image = header
        if not image and is_package and pkg_info:
            image = pkg_info.get("header_image") or ""
        if not image and getattr(game, "appid", None):
            aid = parent_appid if (is_package and parent_appid) else str(game.appid)
            if aid:
                try:
                    image = await self.resolve_steam_cover_url(aid) or ""
                except Exception:
                    image = ""
                if not image:
                    # 最后兜底：新版 store_item_assets + 旧路径都试
                    from ...infrastructure.clients.steam import steam_cover_candidates
                    for u in steam_cover_candidates(aid)[:4]:
                        image = u
                        break
        # 发行商说明
        if getattr(game, "publisher_tag", ""):
            lines.insert(1, f"🏪 发行商：{game.publisher_tag}")
            msg = "\n".join(lines)
        logger.info(f"[查价] 完成 game={getattr(game,'title',None)!r} appid={getattr(game,'appid',None)} lines={len(lines)} image={'Y' if image else 'N'} pub={getattr(game,'publisher_tag',None)}")
        return {
            "image": image,
            "msg": msg,
            "title": str(getattr(game, "title", None) or ""),
            "appid": str(getattr(game, "appid", None) or ""),
            "is_package": is_package,
            "parent_appid": parent_appid,
            "sale_end_ts": sale_end_ts,
            "sale_end_text": sale_end_text,
            "sale_end_line": sale_end_line,
            "publisher_tag": getattr(game, "publisher_tag", ""),
        }



    async def _emit_price_for_game(self, event, game, *, clear_pending: bool = False, session_key=None, compact: bool = False):
        """单款：查完整价格并直接发出。"""
        payload = await self._collect_price_for_game(game, compact=compact)
        if clear_pending and session_key:
            self._steam_search_pending.pop(session_key, None)
            self._steam_search_cache.pop(session_key, None)
        msg = (payload or {}).get("msg") or "查询失败"
        image = (payload or {}).get("image") or ""
        try:
            if image:
                yield event.chain_result([Image.fromURL(image), Plain(msg)])
            else:
                yield event.plain_result(msg)
        except Exception as exc:
            logger.exception("价格文本发送失败: %s", exc)
            yield event.plain_result(msg)
        return


    async def _steam_price(self, event: AstrMessageEvent, auto_first: bool, prefix: str, query_override: str = None):
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        """按中文名、英文名或 Steam 链接查询当前价格与历史最低价（px 为 auto_first 快捷版）。

        候选列表支持多序号连查：回复「1 2」「1、3」一次查多款；
        查询后缓存保留，可继续回复其它序号。
        进度提示走 _price_ack（context 发送），最终结果才 yield。
        """
        if query_override is not None and str(query_override).strip():
            query = str(query_override).strip()
        else:
            raw_msg = getattr(event, "message_str", None)
            if raw_msg is None:
                getter = getattr(event, "get_message_str", None)
                raw_msg = getter() if callable(getter) else ""
            query = extract_price_query(str(raw_msg or ""), prefix)
        if not query:
            yield event.plain_result(f"用法：/steam {prefix} <游戏名或 Steam 链接>\n多选：出现列表后回复「1 2」或「1、2」")
            return
        session_key = self._steam_search_session_key(event)
        # 含中文的查询 = 新搜索，不得再套用上一轮候选序号
        query_is_index = bool(self._parse_price_indices(query)) and not self._has_cjk_text(str(query))
        if query and self._has_cjk_text(str(query)):
            self._clear_price_search_cache(session_key)
            pending = None
        else:
            self._expire_price_search_cache(session_key)
        pending = self._steam_search_pending.get(session_key)
        selected_from_cache = False
        selected_games = []

        if query_is_index:
            cache_games = self._steam_search_cache.get(session_key, []) or []
            age = self._price_cache_age(session_key)
            ttl_min = int(round(self._price_cache_ttl() / 60.0)) or 10
            if not cache_games:
                age_txt = ""
                if age is not None:
                    age_txt = f"（约 {int(age/60)} 分钟前过期，有效期 {ttl_min} 分钟）"
                yield event.plain_result(
                    f"查价候选序号已过期或不存在{age_txt}。\n"
                    f"请重新发送：/price 游戏名\n"
                    f"候选列表有效期约 {ttl_min} 分钟（可在配置里改 price_search_cache_sec）。"
                )
                return
            if not pending:
                yield event.plain_result(
                    f"查价候选序号已过期（有效期约 {ttl_min} 分钟），请重新 /price 游戏名。"
                )
                return
            indices = self._parse_price_indices(query)
            if indices and cache_games:
                bad = []
                for n in indices:
                    if 1 <= n <= len(cache_games):
                        g = cache_games[n - 1]
                        if g not in selected_games:
                            selected_games.append(g)
                    else:
                        bad.append(str(n))
                if bad:
                    await self._price_ack(event, f"序号无效：{'、'.join(bad)}（共 {len(cache_games)} 项）")
                if not selected_games:
                    return
                selected_from_cache = True
                # 选号后刷新时间戳，允许短时间内连续选其它序号
                self._set_price_search_cache(session_key, cache_games)
                logger.info(f"[查价] 序号选择 count={len(selected_games)} query={query!r}")
            for _g in selected_games:
                try:
                    await self._ensure_game_appid(_g)
                except Exception:
                    pass

        if not selected_from_cache:
            if query_is_index:
                # 已在上面处理过期/无效，不再把「1」当游戏名搜
                return
            # 新搜索前清掉旧候选，失败后也不会误选上一轮
            self._clear_price_search_cache(session_key)
            await self._price_ack(event, f"正在搜索「{query}」相关游戏，请稍等...")
            games = await self.ITAD_CLIENT.search_games(query, limit=10)
            if not games and self._contains_chinese(query):
                translated = await self._translate_game_query(query)
                if translated and translated != query:
                    games = await self.ITAD_CLIENT.search_games(translated, limit=10)
            # 中文仍无结果：按词根拆分再搜（商店已 403 时不要再狂打）
            if not games and self._contains_chinese(query) and not steam_store_blocked():
                bits = re.findall(r"[一-鿿]{2,}", str(query))
                bits = [b for b in bits if len(b) >= 2][:3]
                merged, seen_app = [], set()
                for bit in bits:
                    sub = await self.ITAD_CLIENT.search_games(bit, limit=6)
                    for g in sub or []:
                        aid = str(getattr(g, "appid", "") or "")
                        if aid and aid not in seen_app:
                            seen_app.add(aid)
                            merged.append(g)
                if merged:
                    games = merged[:10]
                    logger.info(f"[查价] 中文分词兜底 {bits} -> {len(games)}")
            if not games:
                yield event.plain_result(
                    "未找到匹配游戏，或 ITAD 暂时无法访问。\n"
                    "提示：可换个更接近 Steam 商店名的说法，或直接发商店链接 / appid。"
                )
                return
            # Steam 搜索 403 时 ITAD 结果常缺 appid → 补全并稳定排序
            games = await self._enrich_search_games(games)
            missing = [str(getattr(g, "title", "")) for g in games if not str(getattr(g, "appid", "") or "").isdigit()]
            if missing:
                logger.warning(f"[查价] 候选仍缺 appid: {missing[:6]}")
            if len(games) == 1:
                self._clear_price_search_cache(session_key)
                selected_games = [games[0]]
                await self._price_ack(event, "已找到 1 款，正在查询价格，请稍等...")
            elif not auto_first and len(games) > 1:
                # 批量 GetItems 校正 type（一次请求；失败则回退标题启发）
                appids = [str(getattr(g, "appid", "") or "") for g in games]
                batch_types = {}
                try:
                    batch_types = await self.fetch_app_types_batch(appids) or {}
                except Exception as e:
                    logger.warning(f"[查价] 候选批量 type 失败: {e}")
                if batch_types:
                    cache = getattr(self, "_app_meta_cache", None)
                    if cache is None:
                        cache = {}
                        self._app_meta_cache = cache
                    for g in games:
                        aid = str(getattr(g, "appid", "") or "")
                        info = batch_types.get(aid) or {}
                        typ = str(info.get("type") or "").lower()
                        name = str(info.get("name") or "")
                        pubs = info.get("publishers") or []
                        try:
                            g.publishers = pubs
                        except Exception:
                            pass
                        if typ:
                            try:
                                g.content_type = typ
                            except Exception:
                                pass
                            cache[aid] = {
                                "type": typ,
                                "name": name or cache.get(aid, {}).get("name", ""),
                                "publishers": pubs,
                            }
                # 豪华版/捆绑包（storesearch type=sub）不当独立候选：
                # 标准流程是查本体，在本体结果的「版本套餐」里看豪华版。
                filtered_games = []
                skipped_pkgs = 0
                for g in games:
                    ctype = str(getattr(g, "content_type", "") or "").lower()
                    name = f"{getattr(g,'title','')} {getattr(g,'title_zh','')}"
                    is_pkg = ctype in {"sub", "package", "bundle"}
                    if not is_pkg:
                        # 名称含数字豪华版等且无可靠 type=game 时，也当套餐跳过
                        if self.ITAD_CLIENT._looks_like_edition(name) and ctype not in {"game", "0"}:
                            is_pkg = True
                    if is_pkg:
                        skipped_pkgs += 1
                        continue
                    filtered_games.append(g)
                if skipped_pkgs:
                    await self._price_ack(
                        event,
                        f"已过滤 {skipped_pkgs} 个捆绑包/豪华版；请查本体，豪华版价在结果的「版本套餐」中。",
                    )
                if not filtered_games:
                    filtered_games = games
                games = filtered_games

                # 中文查询：优先云豹/国区中文版，避免默认美版（GungHo）
                def _cn_pref(g):
                    ctype = str(getattr(g, "content_type", "") or "").lower()
                    if ctype in {"sub", "package", "bundle", "dlc", "4"}:
                        return (3, "", str(getattr(g, "title", "")))
                    tag = str(getattr(g, "publisher_tag", "") or "")
                    if not tag:
                        pubs = " ".join(getattr(g, "publishers", None) or []).lower()
                        if "clouded" in pubs:
                            tag = "云豹"
                        elif "gungho" in pubs or "america" in pubs:
                            tag = "美版"
                    zh = str(getattr(g, "title_zh", "") or "")
                    name = str(getattr(g, "title", "") or "")
                    has_zh = 0 if (self._has_cjk_text(zh) or self._has_cjk_text(name)) else 1
                    if tag == "云豹":
                        rank = 0
                    elif tag == "美版":
                        rank = 2
                    else:
                        rank = 1
                    return (rank, has_zh, name)

                if self._has_cjk_text(query):
                    games = sorted(games, key=_cn_pref)
                    logger.info(f"[查价] 中文查询排序后: "
                                f"{[(getattr(g,'appid',None), getattr(g,'publisher_tag',None), getattr(g,'title',None)) for g in games[:6]]}")

                annotated = []
                for game in games:
                    display, tags = await self._format_game_search_item(game)
                    zh_name = str(getattr(game, "title_zh", "") or "")
                    extra_name = f"{display} {zh_name}"
                    ctype = str(getattr(game, "content_type", "") or "").lower()
                    is_extra = (
                        any(t in tags for t in ("DLC", "版本包"))
                        or ctype in {"dlc", "dlc_detail", "music", "episode", "demo", "mod", "4"}
                        or self.ITAD_CLIENT._is_dlc_like_name(extra_name, ctype)
                    )
                    if ctype in {"game", "0"} and "DLC" not in tags and "版本包" not in tags:
                        is_extra = False
                    annotated.append({"game": game, "display": display, "tags": tags, "extra": is_extra})
                base_items = [a for a in annotated if not a["extra"]]
                extra_items = [a for a in annotated if a["extra"]]
                MAX_DLC_SHOWN = 3
                shown = base_items + extra_items[:MAX_DLC_SHOWN]
                hidden_dlc = max(0, len(extra_items) - MAX_DLC_SHOWN)
                if not base_items and extra_items:
                    shown = extra_items[:6]
                    hidden_dlc = max(0, len(extra_items) - len(shown))
                displayed_games = [a["game"] for a in shown]
                self._set_price_search_cache(session_key, displayed_games)
                ttl_min = int(round(self._price_cache_ttl() / 60.0)) or 3
                lines = [
                    f"找到多个匹配游戏，请【引用】本条消息后回复序号（如引用后发 1 或 1 2）；"
                    f"序号约 {ttl_min} 分钟内有效："
                ]
                for index, a in enumerate(shown, 1):
                    display = a["display"]
                    if a["tags"]:
                        display = f"{display} [ {' / '.join(a['tags'])} ]"
                    lines.append(f"{index}. {display}")
                if hidden_dlc > 0:
                    lines.append(f"…另有 {hidden_dlc} 个 DLC/套组未列出（可搜更具体版本名，如「xxx 豪华版」）")
                yield event.plain_result("\n".join(lines))
                return
            else:
                selected_games = [games[0]]
                await self._price_ack(event, "正在查询价格，请稍等...")

        MAX_MULTI = 3
        if len(selected_games) > MAX_MULTI:
            await self._price_ack(event, f"一次最多查询 {MAX_MULTI} 款，已只查前 {MAX_MULTI} 个。")
            selected_games = selected_games[:MAX_MULTI]

        self._price_call_cache = {}
        keep_pending = selected_from_cache
        multi = len(selected_games) > 1

        if not multi:
            g = selected_games[0]
            payload = await self._collect_price_for_game(g, compact=False)
            if not keep_pending and session_key:
                self._clear_price_search_cache(session_key)
            msg = (payload or {}).get("msg") or "查询失败"
            image = (payload or {}).get("image") or ""
            try:
                if image:
                    yield event.chain_result([Image.fromURL(image), Plain(msg)])
                else:
                    yield event.plain_result(msg)
            except Exception as exc:
                logger.exception("价格文本发送失败: %s", exc)
                yield event.plain_result(msg)
        else:
            temp = []
            await self._price_ack(
                event,
                f"将按完整规则查询 {len(selected_games)} 款（含多区），逐款缓存中，请稍等...",
            )
            for idx, g in enumerate(selected_games, 1):
                logger.info(f"[查价] 连查 {idx}/{len(selected_games)} {getattr(g,'title',None)!r} appid={getattr(g,'appid',None)}")
                try:
                    p = await self._collect_price_for_game(g, compact=False)
                except Exception as e:
                    logger.exception(f"[查价] 连查第{idx}款异常: {e}")
                    p = {"image": "", "msg": f"🎮《{getattr(g,'title','?')}》\n查询失败：{e}", "title": str(getattr(g,'title','?'))}
                temp.append(p or {"image": "", "msg": "查询失败", "title": ""})
                logger.info(f"[查价] 连查缓存 {idx}/{len(selected_games)}")

            parts = [f"📦 批量查价 · 共 {len(temp)} 款"]
            chain_items = [Plain(parts[0])]
            for i, p in enumerate(temp, 1):
                parts.append("")
                parts.append(f"━━ {i}/{len(temp)} ━━")
                body = (p.get("msg") or "").strip()
                parts.append(body)
                section = f"\n━━ {i}/{len(temp)} ━━\n{body}"
                img_url = p.get("image") or ""
                if not img_url and p.get("appid"):
                    try:
                        img_url = await self.resolve_steam_cover_url(p["appid"]) or ""
                    except Exception:
                        img_url = ""
                    if not img_url:
                        from ...infrastructure.clients.steam import steam_cover_candidates
                        cands = steam_cover_candidates(p["appid"])
                        img_url = cands[0] if cands else ""
                # 每款都带上自己的封面
                if img_url:
                    chain_items.append(Image.fromURL(img_url))
                chain_items.append(Plain(section))
            joined = "\n".join(parts)
            try:
                import os as _os
                cache_path = _os.path.join(self.data_dir, "price_multi_last.json")
                with open(cache_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "time": datetime.now().isoformat(timespec="seconds"),
                        "count": len(temp),
                        "items": [{
                            "title": p.get("title"),
                            "appid": p.get("appid"),
                            "image": p.get("image"),
                            "msg": p.get("msg"),
                        } for p in temp],
                    }, f, ensure_ascii=False, indent=2)
            except Exception as e:
                logger.warning(f"[查价] 连查临时缓存写入失败: {e}")
            try:
                if len(chain_items) > 1:
                    yield event.chain_result(chain_items)
                else:
                    yield event.plain_result(joined)
            except Exception as exc:
                logger.exception("批量价格拼装发送失败: %s", exc)
                yield event.plain_result(joined)

        if keep_pending and selected_games:
            total = len(self._steam_search_cache.get(session_key, []) or [])
            yield event.plain_result(f"（候选仍有效，共 {total} 项，可继续回复序号，如 2 或 3）")
        return

    async def _steam_lookup_impl(self, event: AstrMessageEvent, query: str = "", target: str = ""):
        '''一站式查询：/steam <游戏名>（多区价格+史低+简介+链接）'''
        logger.info(f"[lookup] steam 泛化指令命中 query={query!r}")
        # 子指令勿当游戏名查价（AstrBot 可能同时命中 /steam 与 /steam xxx）
        _q = (query or "").strip()
        _head = _q.split()[0].lower() if _q else ""
        # /steam who @某人 -> 等价 /steamwho @某人（否则会被当成游戏名查价）
        if _head in ("who", "steamwho", "在干嘛"):
            tgt = (target or "").strip()
            if not tgt:
                yield event.plain_result("用法：/steam who @某人（等价于 /steamwho @某人）")
                return
            async for _r in self.steam_who(event, tgt):
                yield _r
            return
        if _head in {
            "help", "menu", "?", "price", "px", "list", "alllist", "config", "set",
            "addid", "delid", "on", "off", "rs", "rank", "allrank", "rank_on",
            "fonts", "openbox", "qq菜单同步", "qq菜单状态", "qq菜单删除",
            "achievement_on", "achievement_off", "test_achievement_render",
            "test_game_start_render", "test_game_end_render", "sim_psn", "sim_xbox",
            "test_xbox_ach", "xbox", "clear_allids", "clear_groupids", "清除缓存",
            "test_perfect", "pr", "test_achievement_render", "test_game_start_render",
            "test_game_end_render", "sim_psn", "sim_xbox",
            "push_group", "delpush_group", "game",
            # 状态/绑定/排行等子指令，禁止被当成游戏名
            "net", "netstatus", "status", "netstat", "bind", "remark", "doc",
            "mybind", "wish", "ach", "lib", "coop", "activity", "time",
            "rank", "allrank", "rank_on",
        }:
            return
        if not _q or _head in ("help", "menu", "?"):
            yield event.plain_result(
                "查价用法：\n"
                "/price <游戏名> —— 多候选序号选择\n"
                "/px <游戏名> —— 快捷版（直接返回第一条）\n"
                "/steam pr <游戏名> —— 同上（简写）\n"
                "/steam px <游戏名> —— 快捷版（直接返回第一条）\n"
                "其他：/steam help 查看全部指令；/steam status 查接口状态"
            )
            return
        # 不再把任意参数当游戏名查价（避免与其他子指令串台）
        yield event.plain_result(
            "未识别的 /steam 子参数：" + _head + "\n"
            "查价请使用 /price <游戏名> 或 /px <游戏名>\n"
            "全部指令见 /steam help"
        )
        return

    async def _steam_price_cmd_impl(self, event: AstrMessageEvent, query: str):
        """价格查询（多个匹配时列出候选并等待回复序号）。纯 Steam 多区。"""
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        async for result in self._steam_price(event, False, "price"):
            yield result

    async def _steam_px_impl(self, event: AstrMessageEvent, query: str):
        """价格查询快捷版（price 缩写）：无需回复序号，直接返回第一条匹配游戏的价格。"""
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        async for result in self._steam_price(event, True, "px"):
            yield result
