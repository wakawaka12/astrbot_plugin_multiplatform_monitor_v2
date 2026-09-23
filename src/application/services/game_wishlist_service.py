"""愿望单 / 打折推送应用服务（从主插件拆出）。

运行时由 SteamStatusMonitorV3 多继承挂载；本文件不注册 AstrBot 指令。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import re
import time
from datetime import date, datetime

from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image, Plain
from PIL import Image as PILImage
import httpx
import requests

from ...infrastructure.clients.steam import format_sale_end_line, steam_store_blocked
from ...infrastructure.persistence import local_store as lstore
from ...shared.logging import logger
from ...shared.network import httpx_client_kwargs, requests_verify, shared_httpx_client
from ...shared.utils.cache_age import format_cache_age


class WishlistServiceMixin:
    """愿望单缓存、打折扫描循环与推送编排。"""

    # ========== 愿望单打折检测与推送 ==========

    def _wish_sale_data_path(self):
        return os.path.join(self.data_dir, "wishlist_sale_data.json")

    def _load_wish_sale_data(self):
        path = self._wish_sale_data_path()
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self.wish_sale_enabled_groups = set(str(x) for x in (data.get("enabled_groups") or []))
                self.wish_sale_last_cuts = data.get("last_cuts") or {}
                self.wish_sale_log = data.get("log") or []
                self.wish_sale_scan_pos = {
                    str(k): int(v or 0) for k, v in (data.get("scan_pos") or {}).items()
                }
                self._wish_store_ban_until = float(data.get("wish_store_ban_until") or 0)
                self._wish_store_403_streak = int(data.get("wish_store_403_streak") or 0)
                self._wish_sale_daily_check_date = str(data.get("daily_check_date") or "")
                self._wish_sale_night_date = str(data.get("night_date") or "")
                self._wish_sale_notify_date = str(data.get("notify_date") or "")
                self._wish_sale_pending_push = list(data.get("pending_push") or [])
        except Exception as e:
            logger.warning(f"[wish_sale] 加载数据失败: {e}")
            self.wish_sale_enabled_groups = set()
            self.wish_sale_last_cuts = {}
            self.wish_sale_log = []
            self.wish_sale_scan_pos = {}
        if not hasattr(self, "wish_sale_scan_pos") or self.wish_sale_scan_pos is None:
            self.wish_sale_scan_pos = {}
        if not hasattr(self, "_wish_sale_daily_check_date"):
            self._wish_sale_daily_check_date = ""
        if not hasattr(self, "_wish_sale_night_date"):
            self._wish_sale_night_date = ""
        if not hasattr(self, "_wish_sale_notify_date"):
            self._wish_sale_notify_date = ""
        if not hasattr(self, "_wish_sale_pending_push") or self._wish_sale_pending_push is None:
            self._wish_sale_pending_push = []
        # 本地库优先补齐（JSON 缺失时）
        try:
            conn = lstore.get_store(getattr(self, "data_dir", "") or "")
            enabled = lstore.get_wish_sale_setting(conn, "enabled_groups")
            if enabled and not self.wish_sale_enabled_groups:
                self.wish_sale_enabled_groups = set(str(x) for x in enabled)
            if not self.wish_sale_last_cuts:
                # 不全量扫表，仅在 JSON 空时尝试 meta 标记的 sid 列表
                sids = lstore.get_meta(conn, "wish_sale_sids") or []
                for sid in sids:
                    cuts = lstore.get_wish_sale_cuts(conn, str(sid))
                    if cuts:
                        self.wish_sale_last_cuts[str(sid)] = cuts
            try:
                pos_meta = lstore.get_meta(conn, "wish_sale_scan_pos")
                if isinstance(pos_meta, dict) and pos_meta and not self.wish_sale_scan_pos:
                    self.wish_sale_scan_pos = {str(k): int(v or 0) for k, v in pos_meta.items()}
            except Exception:
                pass
        except Exception as e:
            logger.debug(f"[local_store] wish_sale load fail: {e}")
        self._wish_price_cache_load()

    def _save_wish_sale_data(self):
        path = self._wish_sale_data_path()
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "enabled_groups": sorted(self.wish_sale_enabled_groups),
                        "last_cuts": self.wish_sale_last_cuts,
                        "scan_pos": getattr(self, "wish_sale_scan_pos", {}) or {},
                        "daily_check_date": getattr(self, "_wish_sale_daily_check_date", "") or "",
                        "night_date": getattr(self, "_wish_sale_night_date", "") or "",
                        "notify_date": getattr(self, "_wish_sale_notify_date", "") or "",
                        "pending_push": getattr(self, "_wish_sale_pending_push", []) or [],
                        "log": self.wish_sale_log[-200:],
                        "wish_store_ban_until": getattr(self, "_wish_store_ban_until", 0),
                        "wish_store_403_streak": getattr(self, "_wish_store_403_streak", 0),
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception as e:
            logger.warning(f"[wish_sale] 保存数据失败: {e}")
        # 双写本地库
        try:
            conn = lstore.get_store(getattr(self, "data_dir", "") or "")
            lstore.set_wish_sale_setting(conn, "enabled_groups", sorted(self.wish_sale_enabled_groups))
            lstore.set_wish_sale_setting(conn, "wish_store_ban_until", getattr(self, "_wish_store_ban_until", 0))
            lstore.set_wish_sale_setting(conn, "wish_store_403_streak", getattr(self, "_wish_store_403_streak", 0))
            lstore.set_wish_sale_setting(conn, "log_tail", self.wish_sale_log[-200:])
            try:
                lstore.set_meta(conn, "wish_sale_scan_pos", getattr(self, "wish_sale_scan_pos", {}) or {})
            except Exception:
                pass
            for sid, cuts in (self.wish_sale_last_cuts or {}).items():
                if isinstance(cuts, dict):
                    lstore.set_wish_sale_cuts(conn, str(sid), cuts)
            lstore.set_meta(conn, "wish_sale_sids", list(self.wish_sale_last_cuts.keys()))
            conn.commit()
        except Exception as e:
            logger.debug(f"[local_store] wish_sale save fail: {e}")

    def _wish_sale_interval_sec(self):
        try:
            hours = float((self.config or {}).get("wish_sale_interval_hours", 6) or 6)
        except (TypeError, ValueError):
            hours = 6.0
        return max(1800, int(hours * 3600))  # 至少 30 分钟

    def _wish_sale_round_limit(self):
        """每轮每个 SID 最多查多少个愿望单 appid（多轮续扫）。"""
        try:
            n = int((self.config or {}).get("wish_sale_round_limit", 50) or 0)
        except (TypeError, ValueError):
            n = 50
        return n if n > 0 else 50

    def _wish_sale_hourly_limit(self):
        """愿望单折扣扫描：后台每小时最多多少次 appdetails（全群合计）。"""
        try:
            n = int((self.config or {}).get("wish_sale_hourly_limit", 400) or 0)
        except (TypeError, ValueError):
            n = 400
        return n if n > 0 else 400

    def _wish_sale_manual_limit(self):
        """单人手动 check 时，商店查询的安全上限（防异常超大缓存）。"""
        try:
            n = int((self.config or {}).get("wish_sale_manual_limit", 400) or 0)
        except (TypeError, ValueError):
            n = 400
        return n if n > 0 else 400

    def _wish_sale_recheck_hours(self):
        """无折扣条目的复查间隔（小时）。截止时间未到的有折扣款直接跳过。"""
        try:
            h = float((self.config or {}).get("wish_sale_recheck_hours", 24) or 24)
        except (TypeError, ValueError):
            h = 24.0
        return max(1.0, h)

    def _wish_sale_skip_before_end_sec(self):
        """折扣截止前多久开始允许重查（默认2小时，避免卡在截止瞬间仍跳过）。"""
        try:
            h = float((self.config or {}).get("wish_sale_skip_before_end_hours", 2) or 2)
        except (TypeError, ValueError):
            h = 2.0
        return max(0.25, h) * 3600

    def _wish_price_cache(self) -> dict:
        if not hasattr(self, "_wish_sale_price_cache") or not isinstance(self._wish_sale_price_cache, dict):
            self._wish_sale_price_cache = {}
        return self._wish_sale_price_cache

    def _wish_price_cache_load(self):
        path = os.path.join(self.data_dir, "wish_sale_price_cache.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    self._wish_sale_price_cache = data
        except Exception as e:
            logger.debug(f"[wish_sale] price_cache load fail: {e}")
        if not hasattr(self, "_wish_sale_price_cache"):
            self._wish_sale_price_cache = {}

    def _wish_price_cache_save(self):
        path = os.path.join(self.data_dir, "wish_sale_price_cache.json")
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(self._wish_sale_price_cache, f, ensure_ascii=False)
        except Exception as e:
            logger.debug(f"[wish_sale] price_cache save fail: {e}")

    def _wish_should_query_appid(self, aid: str, now: float | None = None) -> bool:
        """是否需要再打商店查这款的折扣。

        - 有折扣且截止时间仍在未来（减去 skip 缓冲）→ **不查**
        - 无折扣且距上次检查 < recheck_hours → **不查**
        - 无缓存 / 折扣已结束 / 超过复查期 → **要查**
        """
        now = time.time() if now is None else now
        rec = self._wish_price_cache().get(str(aid)) or {}
        if not rec:
            return True
        end_ts = rec.get("sale_end_ts")
        try:
            end_ts = float(end_ts) if end_ts else None
        except (TypeError, ValueError):
            end_ts = None
        cut = int(rec.get("cut") or 0)
        # 截止时间未到：跳过
        if cut > 0 and end_ts and end_ts > now + self._wish_sale_skip_before_end_sec():
            return False
        checked = rec.get("checked_at")
        try:
            checked = float(checked) if checked else None
        except (TypeError, ValueError):
            checked = None
        # 无折扣：复查期内跳过
        if cut <= 0 and checked and (now - checked) < self._wish_sale_recheck_hours() * 3600:
            return False
        return True

    def _wish_filter_appids_to_query(self, appids) -> list:
        now = time.time()
        out = []
        for aid in appids or []:
            if self._wish_should_query_appid(aid, now):
                out.append(str(aid))
        return out

    def _wish_update_price_cache(self, aid: str, info: dict, sale_end_ts=None):
        rec = self._wish_price_cache().get(str(aid)) or {}
        cut = int((info or {}).get("cut") or 0)
        rec.update(
            {
                "cut": cut,
                "current_price": (info or {}).get("current_price"),
                "currency": (info or {}).get("currency") or "CNY",
                "name": (info or {}).get("name") or rec.get("name") or "",
                "checked_at": time.time(),
            }
        )
        if sale_end_ts:
            rec["sale_end_ts"] = sale_end_ts
        elif cut <= 0:
            rec["sale_end_ts"] = None
        self._wish_price_cache()[str(aid)] = rec

    def _wish_sale_daily_check_hour(self):
        """每天固定复查「无折扣」条目的小时（0-23）；负数=关闭。"""
        try:
            n = int((self.config or {}).get("wish_sale_daily_check_hour", 12) or 0)
        except (TypeError, ValueError):
            n = 12
        return n

    def _wish_filter_daily_recheck(self, appids) -> list:
        """每日12点复查用：只保留「无折扣 / 折扣已结束 / 无缓存」的 appid。

        有折扣且截止时间仍在未来的 **不查**。
        """
        now = time.time()
        skip_pad = self._wish_sale_skip_before_end_sec()
        out = []
        for aid in appids or []:
            aid = str(aid)
            rec = self._wish_price_cache().get(aid) or {}
            if not rec:
                out.append(aid)
                continue
            cut = int(rec.get("cut") or 0)
            end_ts = rec.get("sale_end_ts")
            try:
                end_ts = float(end_ts) if end_ts else None
            except (TypeError, ValueError):
                end_ts = None
            if cut > 0 and end_ts and end_ts > now + skip_pad:
                continue
            out.append(aid)
        return out

    def _wish_sale_night_hour(self):
        """愿望单夜间任务小时（默认1点）；-1=关闭。拉列表+ITAD折扣+限量商店截止。"""
        try:
            n = int((self.config or {}).get("wish_sale_night_hour", 1) or 0)
        except (TypeError, ValueError):
            n = 1
        return n

    def _wish_sale_notify_hour(self):
        """白天统一通知小时（夜里只写库/缓存，白天合并推送）。"""
        try:
            n = int((self.config or {}).get("wish_sale_notify_hour", 9) or 0)
        except (TypeError, ValueError):
            n = 9
        return n

    def _wish_pending_push(self) -> list:
        if not hasattr(self, "_wish_sale_pending_push") or self._wish_sale_pending_push is None:
            self._wish_sale_pending_push = []
        return self._wish_sale_pending_push

    def _wish_enqueue_pending(self, entries):
        pend = self._wish_pending_push()
        for e in entries or []:
            pend.append(e)
        # 只留最近 500 条
        if len(pend) > 500:
            self._wish_sale_pending_push = pend[-500:]

    def _wish_night_store_limit(self):
        """夜间任务里商店请求上限（只用于 SSR 分页 + 折扣截止确认）。"""
        try:
            n = int((self.config or {}).get("wish_sale_night_store_limit", 60) or 0)
        except (TypeError, ValueError):
            n = 60
        return n if n > 0 else 60

    def _wish_itad_sleep(self):
        try:
            return max(0.15, float((self.config or {}).get("wish_sale_itad_sleep", 0.35) or 0.35))
        except (TypeError, ValueError):
            return 0.35

    def _wish_itad_client(self):
        return getattr(self, "ITAD_CLIENT", None)

    async def _wish_itad_price_one(self, aid: str, country: str = "CN"):
        """ITAD：appid → {cut, current_price, currency, name, itad_id}；失败 {}。"""
        itad = self._wish_itad_client()
        if not itad:
            return {}
        try:
            mapped = await itad.lookup_by_appid(str(aid)) or {}
        except Exception as e:
            logger.debug(f"[wish_itad] lookup {aid}: {e}")
            return {}
        gid = str(mapped.get("id") or "")
        name = str(mapped.get("title") or "")
        if not gid:
            return {"cut": 0, "current_price": None, "currency": "CNY", "name": name, "itad_id": ""}
        try:
            sm = await itad.get_price_summary(gid, country) or {}
        except Exception as e:
            logger.debug(f"[wish_itad] summary {aid}/{gid}: {e}")
            return {"cut": 0, "current_price": None, "currency": "CNY", "name": name, "itad_id": gid}
        cut = sm.get("cut")
        try:
            cut = int(cut or 0)
        except (TypeError, ValueError):
            cut = 0
        return {
            "cut": cut,
            "current_price": sm.get("current_price"),
            "current_regular": sm.get("current_regular"),
            "currency": sm.get("currency") or "CNY",
            "name": name or sm.get("name") or "",
            "itad_id": gid,
            "steam_store_low": sm.get("steam_store_low"),
        }

    async def _wish_itad_check_many(self, appids, country: str = "CN", progress_cb=None):
        """批量 ITAD 查折扣（不打 Steam 商店）。返回 {appid: info}。"""
        out = {}
        sleep_s = self._wish_itad_sleep()
        ids = list(appids or [])
        n = len(ids)
        for i, aid in enumerate(ids):
            itad = self._wish_itad_client()
            if itad is not None and getattr(itad, "_itad_blocked", None) and itad._itad_blocked():
                logger.warning(f"[wish_itad] ITAD 熔断中，停止本批 ({i}/{n})")
                break
            aid = str(aid)
            info = await self._wish_itad_price_one(aid, country)
            if info:
                out[aid] = info
            step = 20 if n > 40 else max(5, n // 3)
            if progress_cb and n and ((i + 1) % step == 0 or (i + 1) == n):
                try:
                    await progress_cb(f"ITAD 查价 {i + 1}/{n}（有结果 {len(out)}）")
                except Exception:
                    pass
            if (i + 1) % 40 == 0:
                logger.info(f"[wish_itad] 进度 {i+1}/{n}")
            await asyncio.sleep(sleep_s)
        return out

    def _wish_sale_auto_pull_wishlist(self) -> bool:
        """是否后台自动从 Steam 拉愿望单列表。默认 False=手动 /game wish_sale update。"""
        try:
            return bool((self.config or {}).get("wish_sale_auto_pull_wishlist", False))
        except Exception:
            return False

    def _wish_sale_auto_discount(self) -> bool:
        """是否后台自动对本地愿望单缓存查折扣（受小时限额约束）。"""
        try:
            return bool((self.config or {}).get("wish_sale_auto_discount", True))
        except Exception:
            return True

    def _wish_quota_state(self):
        if not hasattr(self, "_wish_store_quota") or not isinstance(self._wish_store_quota, dict):
            self._wish_store_quota = {"hour_ts": 0, "used": 0}
        return self._wish_store_quota

    def _wish_quota_left(self) -> int:
        """当前自然小时剩余的 appdetails 额度。"""
        st = self._wish_quota_state()
        hour_ts = int(time.time() // 3600)
        if st.get("hour_ts") != hour_ts:
            st["hour_ts"] = hour_ts
            st["used"] = 0
        limit = self._wish_sale_hourly_limit()
        used = int(st.get("used") or 0)
        return max(0, limit - used)

    def _wish_quota_consume(self, n: int) -> int:
        """扣减额度，返回实际扣掉的数量。"""
        st = self._wish_quota_state()
        left = self._wish_quota_left()
        take = max(0, min(int(n or 0), left))
        st["used"] = int(st.get("used") or 0) + take
        return take

    def _wish_sale_loop_sleep_sec(self):
        """折扣检查循环间隔：优先 wish_sale_round_interval_hours。"""
        try:
            rh = float((self.config or {}).get("wish_sale_round_interval_hours", 0) or 0)
        except (TypeError, ValueError):
            rh = 0.0
        if rh > 0:
            return max(900, int(rh * 3600))
        return max(1800, min(self._wish_sale_interval_sec(), 2 * 3600))

    def _wish_sale_wishlist_ttl_sec(self):
        """愿望单缓存 TTL：仅在自动拉列表时用于判断是否过期；手动模式不主动 SSR。"""
        try:
            h = float((self.config or {}).get("wish_sale_cache_ttl_hours", 0) or 0)
        except (TypeError, ValueError):
            h = 0.0
        if h > 0:
            return max(3600, int(h * 3600))
        return max(self._wish_sale_interval_sec(), 24 * 3600)

    @staticmethod
    def _wish_sale_ordered_appids(items):
        """愿望单稳定排序：priority 优先，再按 appid。"""
        items = list(items or [])

        def _key(it):
            pr = it.get("priority")
            return (
                pr is None,
                int(pr) if pr is not None else 0,
                str(it.get("appid") or ""),
            )

        items.sort(key=_key)
        out = []
        seen = set()
        for it in items:
            aid = str(it.get("appid") or "").strip()
            if aid.isdigit() and aid not in seen:
                seen.add(aid)
                out.append(aid)
        return out

    @staticmethod
    def _wish_sale_take_slice(sid, appids, limit, scan_pos):
        """从游标处取本轮 appid 切片，返回 (batch, new_pos, wrapped)。"""
        if not appids:
            return [], 0, False
        n_total = len(appids)
        limit = max(1, int(limit or 1))
        pos = int(scan_pos.get(sid, 0) or 0) % n_total
        n = min(limit, n_total)
        if pos + n <= n_total:
            batch = appids[pos : pos + n]
            new_pos = (pos + n) % n_total
            wrapped = new_pos <= pos
        else:
            batch = appids[pos:] + appids[: n - (n_total - pos)]
            new_pos = (pos + n) % n_total
            wrapped = True
        return batch, new_pos, wrapped

    def _wish_sale_min_cut(self):
        try:
            return int((self.config or {}).get("wish_sale_min_cut", 1) or 0)
        except (TypeError, ValueError):
            return 1

    def _wish_sale_max_games(self):
        try:
            n = int((self.config or {}).get("wish_sale_max_games", 120) or 0)
        except (TypeError, ValueError):
            n = 120
        return n if n > 0 else 0

    def _wish_sale_country(self):
        return str((self.config or {}).get("wish_sale_country", "CN") or "CN")

    def _wish_sale_regions(self):
        """折扣推送展示的区服列表（最多 4 个，与查价配置对齐）。"""
        primary = self._wish_sale_country().strip().upper() or "CN"
        compare_raw = str((self.config or {}).get("price_compare_regions", "IN,UA,PK") or "").strip()
        compare = []
        if compare_raw and compare_raw.upper() != "NONE":
            compare = [r.strip().upper() for r in compare_raw.split(",") if r.strip().upper()]
        if not compare:
            # 查价未配对比区时，给一组常用四区
            compare = ["US", "JP", "HK"]
        codes = [primary]
        for r in compare:
            if r not in codes:
                codes.append(r)
            if len(codes) >= 4:
                break
        return codes[:4]

    @staticmethod
    def _wish_region_label(cc: str) -> str:
        table = {
            "CN": "国区", "UA": "乌克兰", "RU": "俄罗斯", "IN": "印度",
            "PK": "南亚", "US": "美区", "JP": "日区", "KR": "韩区",
            "TR": "土区", "HK": "港区", "SG": "新加坡", "GB": "英国",
        }
        cc = str(cc or "").upper()
        return table.get(cc, cc)

    async def _wish_sale_region_prices(self, appid: str):
        """为单款游戏拉取最多 4 个区的现价/折扣（查价精简版，不含史低）。"""
        from ...presentation.renderers.game_detail import CURRENCY_SYMBOL
        from ...shared.utils.price import to_cny
        regions = self._wish_sale_regions()
        out = {}

        async def _one(cc):
            try:
                info = await self.fetch_region_price(appid, cc)
            except Exception:
                info = None
            label = self._wish_region_label(cc)
            if not info:
                out[label] = {"ok": False, "cut": 0, "current_price": None, "currency": None, "current_cny": None}
                return
            currency = str(info.get("currency") or "CNY")
            price = info.get("current_price")
            cut = int(info.get("cut") or 0)
            cny = to_cny(price, currency) if price is not None else None
            sym = CURRENCY_SYMBOL.get(currency.upper(), "")
            out[label] = {
                "ok": price is not None,
                "cut": cut,
                "current_price": price,
                "current_regular": info.get("current_regular"),
                "currency": currency,
                "current_cny": cny,
                "symbol": sym,
                "cc": cc,
            }

        await asyncio.gather(*(_one(cc) for cc in regions))
        # 按配置顺序输出
        ordered = {}
        for cc in regions:
            label = self._wish_region_label(cc)
            if label in out:
                ordered[label] = out[label]
        return ordered

    def _wish_sale_sids(self):
        """开启打折推送的群所监控的纯 SteamID64。"""
        sids = []
        seen = set()
        for gid in self.wish_sale_enabled_groups:
            for sid in (self.group_steam_ids.get(gid) or []):
                s = str(sid)
                if s.isdigit() and len(s) == 17 and s not in seen:
                    seen.add(s)
                    sids.append(s)
        return sids

    def _wish_bound_steam_sids(self):
        """需要维护本地愿望单缓存的用户：QQ绑定 + 监控列表中的 SteamID64。"""
        sids = []
        seen = set()

        def _add(raw):
            s = str(raw or "").strip()
            if s.isdigit() and len(s) == 17 and s not in seen:
                seen.add(s)
                sids.append(s)

        for s in self._wish_sale_sids():
            _add(s)
        bind = getattr(self, "_bind_data", {}) or {}
        for qq, info in bind.items():
            if str(qq).startswith("__remark:"):
                continue
            if not isinstance(info, dict):
                continue
            for s in (info.get("sids") or ([info.get("sid")] if info.get("sid") else [])):
                _add(s)
        # 其余监控群里的 Steam 玩家也进缓存（便于日后开启 wish_sale 即用）
        for ids in (self.group_steam_ids or {}).values():
            for sid in ids:
                _add(sid)
        return sids

    def _wish_cache_dir(self):
        path = os.path.join(self.data_dir, "wishlist_cache")
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    def _wish_cache_path(self, sid: str) -> str:
        return os.path.join(self._wish_cache_dir(), f"{sid}.json")

    def _load_wishlist_cache(self, sid: str):
        path = self._wish_cache_path(sid)
        try:
            if not os.path.exists(path):
                return None
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return None
            return data
        except Exception as e:
            logger.debug(f"[wish_cache] 读取失败 sid={sid}: {e}")
            return None

    def _save_wishlist_cache(self, sid: str, data: dict):
        path = self._wish_cache_path(sid)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            os.replace(tmp, path)
        except Exception as e:
            logger.warning(f"[wish_cache] 保存失败 sid={sid}: {e}")

    def _truncate_wishlist_items(self, items):
        items = list(items or [])
        max_n = self._wish_sale_max_games()
        if max_n and len(items) > max_n:
            items = sorted(
                items,
                key=lambda x: (
                    x.get("priority") is None,
                    x.get("priority") if x.get("priority") is not None else 0,
                ),
            )[:max_n]
        return items

    def _wishlist_item_names(self, items, appids):
        name_by = {}
        for it in items or []:
            name_by[str(it.get("appid") or "")] = it.get("name") or ""
        out = []
        for aid in appids:
            out.append(name_by.get(str(aid)) or f"appid {aid}")
        return out

    async def _refresh_wishlist_caches(self, sids=None, force: bool = False):
        """把绑定/监控用户愿望单写入本地缓存；到期才打 Steam SSR，并 diff 变化。

        返回事件列表：
          [{"type":"cached"|"refreshed"|"changed"|"refresh_failed"|"cooldown", sid, ...}]
        """
        sids = sids if sids is not None else self._wish_bound_steam_sids()
        ttl = self._wish_sale_interval_sec()  # 默认 6h
        now = time.time()
        events = []
        for sid in sids:
            try:
                ban_until = float(getattr(self, "_wish_store_ban_until", 0) or 0)
            except (TypeError, ValueError):
                ban_until = 0.0
            if ban_until and now < ban_until:
                left = int(ban_until - now)
                logger.warning(f"[wish_cache] SSR冷却中，跳过刷新（剩 {left}s） sid={sid}")
                events.append({
                    "type": "cooldown",
                    "sid": sid,
                    "player_name": self._resolve_player_display_name(sid),
                    "left_sec": left,
                })
                break
            old = self._load_wishlist_cache(sid)
            age = now - float((old or {}).get("fetched_at") or 0)
            has_old = bool(old) and old.get("items") is not None
            if not force and has_old and age < ttl:
                # 缓存未过期：不打 Steam
                self._wish_sale_cache[sid] = {
                    "ts": float(old.get("fetched_at") or now),
                    "items": old.get("items") or [],
                }
                continue
            player_name = self._resolve_player_display_name(sid)
            items = await self.fetch_public_wishlist(sid)
            if items is None:
                if has_old:
                    logger.warning(f"[wish_cache] 刷新失败，保留旧缓存 sid={sid} n={len(old.get('items') or [])}")
                    events.append({
                        "type": "refresh_failed_stale",
                        "sid": sid,
                        "player_name": player_name,
                        "count": len(old.get("items") or []),
                        "age_min": int(age // 60),
                    })
                else:
                    events.append({"type": "refresh_failed", "sid": sid, "player_name": player_name})
                await asyncio.sleep(10)
                continue
            items = self._truncate_wishlist_items(items)
            old_items = (old or {}).get("items") or []
            old_ids = [str(x.get("appid")) for x in old_items if x.get("appid")]
            new_ids = [str(x.get("appid")) for x in items if x.get("appid")]
            added = [a for a in new_ids if a not in old_ids]
            removed = [a for a in old_ids if a not in new_ids]
            data = {
                "sid": sid,
                "player_name": player_name,
                "fetched_at": time.time(),
                "items": items,
                "item_appids": new_ids,
                "count": len(items),
                "prev_count": len(old_ids),
                "last_added": added,
                "last_removed": removed,
            }
            self._save_wishlist_cache(sid, data)
            self._wish_sale_cache[sid] = {"ts": data["fetched_at"], "items": items}
            if not has_old:
                events.append({
                    "type": "cached",
                    "sid": sid,
                    "player_name": player_name,
                    "count": len(items),
                })
                logger.info(f"[wish_cache] 首次写入 sid={sid} n={len(items)} player={player_name}")
            elif added or removed:
                events.append({
                    "type": "changed",
                    "sid": sid,
                    "player_name": player_name,
                    "count": len(items),
                    "prev_count": len(old_ids),
                    "added": added,
                    "removed": removed,
                    "added_names": self._wishlist_item_names(items, added),
                    "removed_names": self._wishlist_item_names(old_items, removed),
                })
                logger.info(
                    f"[wish_cache] 变化 sid={sid} +{len(added)} -{len(removed)} total={len(items)}"
                )
            else:
                events.append({
                    "type": "refreshed",
                    "sid": sid,
                    "player_name": player_name,
                    "count": len(items),
                })
                logger.info(f"[wish_cache] 无变化刷新 sid={sid} n={len(items)}")
            # SSR 间隔，避免连打
            await asyncio.sleep(12)
        return events

    async def _push_wishlist_changes(self, events):
        """推送愿望单清单变化（新增/移除），按人合并；仅发到开启 wish_sale 的群。"""
        if not events:
            return
        interesting = [e for e in events if e.get("type") == "changed"]
        if not interesting:
            return
        by_umo = {}
        for e in interesting:
            sid = e.get("sid")
            for umo in self._wish_sale_push_targets(sid) or []:
                by_umo.setdefault(umo, []).append(e)
        for umo, items in by_umo.items():
            lines = ["📋 愿望单清单变化"]
            for e in items:
                name = e.get("player_name") or e.get("sid")
                lines.append(f"· {name}：现有 {e.get('count')} 条（原 {e.get('prev_count')}）")
                if e.get("added_names"):
                    lines.append(f"  ＋新增：{'、'.join(e['added_names'][:8])}" + ("…" if len(e['added_names']) > 8 else ""))
                if e.get("removed_names"):
                    lines.append(f"  －移除：{'、'.join(e['removed_names'][:8])}" + ("…" if len(e['removed_names']) > 8 else ""))
            try:
                await self.context.send_message(umo, MessageChain().message("\n".join(lines)))
            except Exception as ex:
                logger.error(f"[wish_cache] 清单变化推送失败 {umo}: {ex}")

    def _wish_sale_push_targets(self, sid: str):
        """愿望单打折推送目标：开启 wish_sale 的群 + 联动推送组。"""
        from ...shared.utils.notify_session import is_sendable_group_session
        umos = []
        notify_sessions = getattr(self, "notify_sessions", {}) or {}

        def _add(umo):
            if umo and umo not in umos and is_sendable_group_session(umo):
                umos.append(umo)

        for gid, ids in (self.group_steam_ids or {}).items():
            if sid not in {str(x) for x in ids}:
                continue
            if str(gid) not in self.wish_sale_enabled_groups:
                continue
            _add(notify_sessions.get(str(gid)))
        for pg in (getattr(self, "push_groups", {}) or {}).get(sid, []) or []:
            if str(pg) in self.wish_sale_enabled_groups:
                _add(notify_sessions.get(str(pg)))
        return umos

    async def _get_wishlist_for_sale(self, sid: str, force: bool = False, cache_only: bool = False):
        """取愿望单。

        - cache_only=True：只读本地缓存，绝不打 Steam SSR（折扣扫描默认走这里）
        - force=True：强制 SSR 拉取（手动 /game wish_sale update）
        - 否则：仅当开了自动拉取且缓存过期才 SSR
        """
        disk = self._load_wishlist_cache(sid)
        mem = self._wish_sale_cache.get(sid)
        now = time.time()
        ttl = self._wish_sale_wishlist_ttl_sec()
        try:
            ban_until = float(getattr(self, "_wish_store_ban_until", 0) or 0)
        except (TypeError, ValueError):
            ban_until = 0.0
        in_ban = bool(ban_until and now < ban_until)

        # 内存极新鲜
        if not force and mem and (now - float(mem.get("ts") or 0)) < 600 and mem.get("items"):
            return mem.get("items") or []

        disk_items = (disk or {}).get("items") if disk else None
        disk_ok = disk_items is not None
        disk_age = now - float((disk or {}).get("fetched_at") or 0) if disk_ok else None

        if not force and disk_ok:
            if disk_age is not None and disk_age < ttl:
                self._wish_sale_cache[sid] = {"ts": float(disk.get("fetched_at") or now), "items": disk_items}
                return disk_items or []
            if cache_only or in_ban or not self._wish_sale_auto_pull_wishlist():
                # 手动模式 / 冷却 / 仅缓存：绝不自动打 SSR
                return disk_items or []

        if cache_only or in_ban:
            return disk_items if disk_ok else None

        if not force and not self._wish_sale_auto_pull_wishlist():
            return disk_items if disk_ok else None

        items = await self.fetch_public_wishlist(sid)
        if items is None:
            if disk_ok:
                logger.warning(f"[wish] SSR失败，使用本地缓存 sid={sid} n={len(disk_items or [])}")
                return disk_items or []
            return None
        items = self._truncate_wishlist_items(items)
        # 写入本地缓存 + diff
        old_ids = [str(x.get("appid")) for x in (disk_items or []) if x.get("appid")]
        new_ids = [str(x.get("appid")) for x in items if x.get("appid")]
        added = [a for a in new_ids if a not in old_ids]
        removed = [a for a in old_ids if a not in new_ids]
        player_name = self._resolve_player_display_name(sid)
        data = {
            "sid": sid,
            "player_name": player_name,
            "fetched_at": time.time(),
            "items": items,
            "item_appids": new_ids,
            "count": len(items),
            "prev_count": len(old_ids),
            "last_added": added,
            "last_removed": removed,
        }
        self._save_wishlist_cache(sid, data)
        self._wish_sale_cache[sid] = {"ts": data["fetched_at"], "items": items}
        return items

    async def _wish_sale_night_job(self):
        """凌晨任务：拉愿望单列表（SSR）→ ITAD 全量扫折扣 → 限量商店补截止。"""
        from datetime import datetime
        now = datetime.now()
        date_key = now.strftime("%Y-%m-%d")
        logger.info(f"[wish_sale] 夜间任务开始 {date_key} night_hour={self._wish_sale_night_hour()}")
        store_budget = self._wish_night_store_limit()
        self._wish_sale_end_budget = max(10, store_budget // 2)
        sids = self._wish_sale_sids()
        if not sids:
            logger.info("[wish_sale] 夜间任务：无 wish_sale SID")
            return
        # 1) 拉列表（愿望单 SSR，与查价错峰；受愿望单商店预算）
        if self._wish_sale_auto_pull_wishlist():
            if steam_store_blocked():
                logger.warning("[wish_sale] 夜间：商店冷却，跳过 SSR 拉列表")
            else:
                ok = fail = 0
                for sid in sids:
                    if self._wish_store_ban_active():
                        logger.warning("[wish_sale] 夜间 SSR 冷却，中止拉列表")
                        break
                    if store_budget <= 0:
                        logger.warning("[wish_sale] 夜间商店预算用尽，停止 SSR")
                        break
                    try:
                        items = await self._get_wishlist_for_sale(sid, force=True, cache_only=False)
                        store_budget -= 1
                        if items is None:
                            fail += 1
                        else:
                            ok += 1
                            logger.info(f"[wish_sale] 夜间SSR sid=...{sid[-4:]} n={len(items)}")
                    except Exception:
                        fail += 1
                        logger.exception(f"[wish_sale] 夜间SSR失败 sid={sid}")
                    await asyncio.sleep(4.0)
                logger.info(f"[wish_sale] 夜间SSR完成 ok={ok} fail={fail}")
                self._save_wish_sale_data()
        else:
            logger.info("[wish_sale] 夜间：auto_pull=false，跳过 SSR，仅用本地列表")

        # 2) 汇总缓存 appid，ITAD 全量扫（不打商店）
        if not self.wish_sale_enabled_groups:
            logger.info("[wish_sale] 夜间：群未开启 wish_sale，跳过折扣")
            return
        country = self._wish_sale_country()
        min_cut = self._wish_sale_min_cut()
        name_map = {}
        cover_map = {}
        sid_of_app = {}
        unique = []
        seen = set()
        for sid in sids:
            items = await self._get_wishlist_for_sale(sid, cache_only=True) or []
            for it in items:
                aid = str(it.get("appid") or "")
                if not aid.isdigit():
                    continue
                if aid not in seen:
                    seen.add(aid)
                    unique.append(aid)
                name_map[aid] = it.get("name") or name_map.get(aid) or ""
                cover_map[aid] = (it.get("cover_urls") or [], it.get("cover_url") or "") if not cover_map.get(aid) else cover_map[aid]
                sid_of_app.setdefault(aid, []).append(sid)
        # 夜间全量：跳过「有折扣且未截止」，其余都用 ITAD 查
        need = self._wish_filter_daily_recheck(unique)
        logger.info(f"[wish_sale] 夜间ITAD：缓存appid={len(unique)} 待查={len(need)}")
        discounts = await self._wish_itad_check_many(need, country=country)
        new_all = []
        sale_count = 0
        # 3) 有折扣：更新缓存 + 必要时商店补截止 + 按人推送升高折扣
        for aid, info in discounts.items():
            cut = int(info.get("cut") or 0)
            if cut < min_cut:
                self._wish_update_price_cache(aid, info, sale_end_ts=None)
                continue
            sale_count += 1
            sale_end_ts = (self._wish_price_cache().get(aid) or {}).get("sale_end_ts")
            if (not sale_end_ts) and self._wish_sale_end_budget > 0 and not steam_store_blocked():
                try:
                    se = await self.fetch_store_sale_end(aid) or {}
                    sale_end_ts = se.get("end_ts")
                    self._wish_sale_end_budget -= 1
                    await asyncio.sleep(1.2)
                except Exception:
                    sale_end_ts = None
            self._wish_update_price_cache(aid, info, sale_end_ts=sale_end_ts)
            for sid in sid_of_app.get(aid, []):
                prev = self.wish_sale_last_cuts.setdefault(sid, {})
                old = int(prev.get(aid) or 0)
                if cut > old:
                    player_name = self._resolve_player_display_name(sid)
                    entry = {
                        "sid": sid,
                        "player_name": player_name,
                        "appid": aid,
                        "name": info.get("name") or name_map.get(aid) or f"appid {aid}",
                        "cut": cut,
                        "current_price": info.get("current_price"),
                        "current_regular": info.get("current_regular"),
                        "currency": info.get("currency") or "CNY",
                        "header_image": "",
                        "cover_urls": cover_map.get(aid, ([], ""))[0],
                        "cover_url": cover_map.get(aid, ([], ""))[1],
                        "region_prices": {},
                        "sale_end_ts": sale_end_ts,
                        "sale_end_text": None,
                        "sale_end_line": format_sale_end_line(sale_end_ts, None),
                        "date": date.today().isoformat(),
                    }
                    new_all.append(entry)
                prev[aid] = max(old, cut)
        self._wish_price_cache_save()
        self._save_wish_sale_data()
        if new_all:
            # 夜间只写库+排队，不立刻刷屏；白天 notify_hour 统一推送
            self._wish_enqueue_pending(new_all)
            for e in new_all:
                self.wish_sale_log.append({
                    "sid": e.get("sid"),
                    "player_name": e.get("player_name"),
                    "appid": e.get("appid"),
                    "name": e.get("name"),
                    "cut": e.get("cut"),
                    "price": e.get("current_price"),
                    "date": e.get("date"),
                })
            self.wish_sale_log = self.wish_sale_log[-200:]
            self._save_wish_sale_data()
            logger.info(f"[wish_sale] 夜间新折扣已入队 {len(new_all)} 条，待白天 {self._wish_sale_notify_hour()} 点通知")
        self._wish_price_cache_save()
        self._save_wish_sale_data()
        logger.info(
            f"[wish_sale] 夜间任务完成 itad_need={len(need)} discounts={len(discounts)} "
            f"on_sale={sale_count} queued={len(new_all)} store_end_left={self._wish_sale_end_budget}"
        )

    async def _wish_sale_flush_day_notify(self):
        """白天：把夜间入队的折扣合并推送（@绑定 + 合并转发）。"""
        pend = list(self._wish_pending_push())
        self._wish_sale_pending_push = []
        if not pend:
            logger.info("[wish_sale] 白天通知：无待推送折扣")
            return
        try:
            await self._push_wishlist_sales(pend)
            logger.info(f"[wish_sale] 白天通知完成 n={len(pend)}")
        except Exception:
            logger.exception("[wish_sale] 白天通知失败，条目已丢弃（可手动 check）")
            # 失败不回写，避免无限重试打爆

    async def _notify_wish_groups(self, text: str):
        """给开启 wish_sale 的群发一条简短提示。"""
        try:
            from ...shared.utils.notify_session import is_sendable_group_session
        except Exception:
            is_sendable_group_session = None
        notify_sessions = getattr(self, "notify_sessions", {}) or {}
        for gid in (self.wish_sale_enabled_groups or set()):
            umo = notify_sessions.get(str(gid)) or f"default:GroupMessage:{gid}"
            if is_sendable_group_session and not is_sendable_group_session(umo):
                continue
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
            except Exception as e:
                logger.debug(f"[wish_sale] 群提示失败 {gid}: {e}")

    def _wish_store_ban_active(self) -> bool:
        try:
            return float(getattr(self, "_wish_store_ban_until", 0) or 0) > time.time()
        except (TypeError, ValueError):
            return False

    async def _wish_sale_loop(self):
        """后台：凌晨1点跑夜间任务；白天不做商店型愿望单扫描（ITAD/手动 check 另走）。"""
        await asyncio.sleep(60)
        while True:
            try:
                from datetime import datetime
                now_dt = datetime.now()
                night_h = self._wish_sale_night_hour()
                date_key = now_dt.strftime("%Y-%m-%d")
                if not hasattr(self, "_wish_sale_night_date"):
                    self._wish_sale_night_date = ""
                if (
                    self.wish_sale_enabled_groups
                    and self._wish_sale_sids()
                    and night_h >= 0
                    and now_dt.hour >= night_h
                    and self._wish_sale_night_date != date_key
                ):
                    if self.steam_guard_active() or self._status_only_mode():
                        logger.info("[wish_sale] 夜间任务推迟：Steam 扫描/status_only")
                        await asyncio.sleep(1800)
                        continue
                    self._wish_sale_night_date = date_key
                    self._save_wish_sale_data()
                    try:
                        await self._notify_wish_groups(
                            f"【愿望单】开始夜间任务：拉列表 + ITAD 查折扣（约凌晨{night_h}点）"
                        )
                    except Exception:
                        pass
                    try:
                        await self._wish_sale_night_job()
                    except Exception:
                        logger.exception("[wish_sale] 夜间任务失败")
                        self._wish_sale_night_date = ""  # 允许下一轮重试
                else:
                    # 白天统一通知
                    notify_h = self._wish_sale_notify_hour()
                    ndate = getattr(self, "_wish_sale_notify_date", "") or ""
                    if (
                        notify_h >= 0
                        and now_dt.hour >= notify_h
                        and ndate != date_key
                        and self._wish_pending_push()
                    ):
                        self._wish_sale_notify_date = date_key
                        self._save_wish_sale_data()
                        try:
                            await self._notify_wish_groups(
                                f"【愿望单】开始发送折扣汇总（{len(self._wish_pending_push())} 条）…"
                            )
                        except Exception:
                            pass
                        await self._wish_sale_flush_day_notify()
                        self._save_wish_sale_data()
                    else:
                        logger.debug(
                            f"[wish_sale] 等待窗口 hour={now_dt.hour} night={night_h} notify={notify_h} "
                            f"pending={len(self._wish_pending_push())}"
                        )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[wish_sale] 轮询异常")
            # 夜间窗口附近查勤一点，白天放宽
            sleep_s = 1800 if (1 <= datetime.now().hour <= 3) else 3600
            logger.info(f"[wish_sale] loop sleep={sleep_s}s night={self._wish_sale_night_hour()} date={getattr(self,'_wish_sale_night_date','')}")
            await asyncio.sleep(sleep_s)

    async def _check_wishlist_sales(
        self,
        force_push_all: bool = False,
        only_sid: str = None,
        only_targets=None,
        cache_only: bool = True,
        daily_recheck: bool = False,
        progress_cb=None,
    ):
        """检查愿望单折扣（默认只读本地愿望单缓存 + 小时限额内查商店）。

        返回 (new_sales, all_sales, stats)。
        force_push_all=True：当前所有折扣都推送（用于 test / on 首次）。
        cache_only=True：愿望单列表不打 Steam SSR。
        daily_recheck=True：只查「无折扣/折扣已结束」的条目（每日12点任务）。
        """
        min_cut = self._wish_sale_min_cut()
        country = self._wish_sale_country()
        sids = [only_sid] if only_sid else self._wish_sale_sids()
        manual_one = bool(only_sid)
        # 全局商店冷却：整轮跳过，禁止继续刷 appdetails
        if steam_store_blocked():
            logger.warning("[wish_sale] Steam 商店全局冷却中，跳过本轮折扣检查")
            return [], [], {"status": "store_blocked", "sid": only_sid or "", "wishlist_count": 0, "priced_count": 0, "sale_count": 0, "unreadable": [], "empty": []}
        if not manual_one and self._wish_quota_left() <= 0:
            logger.warning(f"[wish_sale] 小时额度用尽 {self._wish_sale_hourly_limit()}，跳过本轮后台折扣检查")
            return [], [], {"status": "quota_exhausted", "sid": only_sid or "", "wishlist_count": 0, "priced_count": 0, "sale_count": 0, "unreadable": [], "empty": []}
        # 商店仅用于确认截止时间；与查价/状态分离的「愿望单池」
        night_cap = self._wish_night_store_limit()
        self._wish_sale_end_budget = max(5, min(30, night_cap // 2)) if not manual_one else max(8, min(40, night_cap))
        all_sales = []
        new_sales = []
        name_map = {}
        cover_map = {}
        stats = {
            "status": "ok",
            "sid": only_sid or "",
            "wishlist_count": 0,
            "priced_count": 0,
            "sale_count": 0,
            "scanned": 0,
            "mode": "manual" if manual_one else ("daily_norecheck" if daily_recheck else "background"),
            "quota_left": 0 if manual_one else self._wish_quota_left(),
            "unreadable": [],
            "empty": [],
        }

        for sid in sids:
            if not manual_one and self._wish_quota_left() <= 0:
                logger.info("[wish_sale] 小时额度用尽，中断本轮剩余 SID")
                stats["status"] = "quota_exhausted"
                break
            # 愿望单 SSR 冷却中：停止本轮继续打商店，避免加重风控
            try:
                ban_until = float(getattr(self, "_wish_store_ban_until", 0) or 0)
            except (TypeError, ValueError):
                ban_until = 0.0
            if ban_until and time.time() < ban_until:
                left = int(ban_until - time.time())
                logger.warning(f"[wish_sale] SSR 冷却中，中断本轮检查（剩 {left}s）")
                stats["status"] = "cooldown"
                break
            items = await self._get_wishlist_for_sale(sid, cache_only=cache_only)
            if items is None:
                logger.warning(f"[wish_sale] 愿望单不可读 sid={sid}")
                stats["unreadable"].append(sid)
                if getattr(self, "_wish_store_ban_until", 0) and time.time() < float(self._wish_store_ban_until):
                    stats["status"] = "cooldown"
                    break
                continue
            if not items:
                stats["empty"].append(sid)
                continue
            stats["wishlist_count"] += len(items)
            ordered_aids = self._wish_sale_ordered_appids(items)
            for it in items:
                aid = str(it.get("appid") or "")
                if aid:
                    name_map[aid] = it.get("name") or ""
                    cover_map[aid] = (it.get("cover_urls") or [], it.get("cover_url") or "")
            if not hasattr(self, "wish_sale_scan_pos") or self.wish_sale_scan_pos is None:
                self.wish_sale_scan_pos = {}
            # 本地库：愿望单 appid 入库
            try:
                conn_w = self._store_conn()
                if conn_w is not None:
                    for aid, nm in name_map.items():
                        if str(aid).isdigit() and nm:
                            lstore.upsert_game(conn_w, appid=str(aid), name=str(nm))
                    conn_w.commit()
            except Exception:
                pass
            if manual_one:
                take_n = min(len(ordered_aids), self._wish_sale_manual_limit())
                batch = ordered_aids[:take_n]
                new_pos = take_n % max(1, len(ordered_aids))
                self.wish_sale_scan_pos[sid] = new_pos
                logger.info(
                    f"[wish_sale] manual sid={sid} 缓存={len(ordered_aids)} 候选={len(batch)} "
                    f"(manual_limit={self._wish_sale_manual_limit()})"
                )
            else:
                if daily_recheck:
                    take_n = min(len(ordered_aids), 250)
                    batch = ordered_aids[:take_n]
                    self.wish_sale_scan_pos[sid] = take_n % max(1, len(ordered_aids))
                else:
                    round_limit = self._wish_sale_round_limit()
                    take_n = min(round_limit, max(0, self._wish_quota_left()))
                    if take_n <= 0:
                        stats["status"] = "quota_exhausted"
                        break
                    batch, new_pos, _wrapped = self._wish_sale_take_slice(
                        sid, ordered_aids, take_n, self.wish_sale_scan_pos
                    )
                    self.wish_sale_scan_pos[sid] = new_pos
                logger.info(
                    f"[wish_sale] sid={sid} 缓存={len(ordered_aids)} 候选={len(batch)} "
                    f"daily={daily_recheck}"
                )
            if not batch:
                continue
            # 截止时间未到 / 复查期内的不打商店
            if daily_recheck:
                need = self._wish_filter_daily_recheck(batch)
            else:
                need = self._wish_filter_appids_to_query(batch)
            skipped = len(batch) - len(need)
            if skipped:
                logger.info(f"[wish_sale] sid={sid} 跳过 {skipped} 条（折扣未截止或复查期内），实查 {len(need)}")
            stats["skipped"] = int(stats.get("skipped") or 0) + skipped
            if not need:
                continue
            if not manual_one:
                if len(need) > self._wish_quota_left():
                    need = need[: max(0, self._wish_quota_left())]
                if not need:
                    stats["status"] = "quota_exhausted"
                    break
                self._wish_quota_consume(len(need))
            stats["scanned"] += len(need)
            pname = self._resolve_player_display_name(sid)
            if progress_cb:
                try:
                    await progress_cb(
                        f"折扣 {pname}（…{sid[-4:]}）：缓存 {len(ordered_aids)} → 实查 {len(need)}（跳过 {len(batch)-len(need)}）"
                    )
                except Exception:
                    pass
            # 主路径：ITAD 判断是否折扣；商店只对「有折扣且缺截止」补 sale_end
            discounts = await self._wish_itad_check_many(
                need, country=country, progress_cb=progress_cb
            )
            if not discounts and not self._wish_itad_client():
                discounts = await self.fetch_app_discounts(need, country=country)
            stats["priced_count"] += len(discounts)
            stats["source"] = "itad" if self._wish_itad_client() else "store"
            prev = {str(k): int(v or 0) for k, v in (self.wish_sale_last_cuts.get(sid) or {}).items()}
            player_name = self._resolve_player_display_name(sid)
            for aid, info in discounts.items():
                cut = int(info.get("cut") or 0)
                if cut < min_cut:
                    prev[aid] = 0
                    self._wish_update_price_cache(aid, info, sale_end_ts=None)
                    continue
                display_name = info.get("name") or name_map.get(aid) or f"appid {aid}"
                # 商店仅确认截止时间（少量）
                sale_end = {}
                sale_end_ts = None
                sale_end_text = None
                sale_end_line = ""
                cached_end = (self._wish_price_cache().get(str(aid)) or {}).get("sale_end_ts")
                old_cut_for_end = int(prev.get(aid) or 0)
                will_push = bool(force_push_all or cut > old_cut_for_end)
                need_end = will_push or not cached_end
                if need_end and not steam_store_blocked() and getattr(self, "_wish_sale_end_budget", 0) > 0:
                    try:
                        sale_end = await self.fetch_store_sale_end(aid) or {}
                        self._wish_sale_end_budget = int(getattr(self, "_wish_sale_end_budget", 0)) - 1
                    except Exception as se_err:
                        logger.debug(f"[wish_sale] 截止时间获取失败 {aid}: {se_err}")
                        sale_end = {}
                    sale_end_ts = sale_end.get("end_ts")
                    sale_end_text = sale_end.get("end_text")
                    sale_end_line = format_sale_end_line(sale_end_ts, sale_end_text)
                    await asyncio.sleep(1.2)
                elif cached_end:
                    sale_end_ts = cached_end
                    sale_end_line = format_sale_end_line(sale_end_ts, None)
                self._wish_update_price_cache(aid, info, sale_end_ts=sale_end_ts)
                region_prices = {}
                try:
                    if will_push:
                        region_prices = await self._wish_sale_region_prices(aid)
                except Exception as rp_err:
                    logger.debug(f"[wish_sale] 多区价格失败 {aid}: {rp_err}")
                max_cut = cut
                for rp in (region_prices or {}).values():
                    try:
                        max_cut = max(max_cut, int((rp or {}).get("cut") or 0))
                    except (TypeError, ValueError):
                        pass
                if max_cut < min_cut:
                    prev[aid] = max_cut
                    continue
                entry = {
                    "sid": sid,
                    "player_name": player_name,
                    "appid": aid,
                    "name": display_name,
                    "cut": max_cut,
                    "current_price": info.get("current_price"),
                    "current_regular": info.get("current_regular"),
                    "currency": info.get("currency") or "CNY",
                    "header_image": info.get("header_image") or "",
                    "cover_urls": cover_map.get(aid, ([], ""))[0],
                    "cover_url": cover_map.get(aid, ([], ""))[1],
                    "region_prices": region_prices,
                    "sale_end_ts": sale_end_ts,
                    "sale_end_text": sale_end_text,
                    "sale_end_line": sale_end_line,
                    "date": date.today().isoformat(),
                }
                all_sales.append(entry)
                old_cut = int(prev.get(aid) or 0)
                if force_push_all or max_cut > old_cut:
                    new_sales.append(entry)
                prev[aid] = max_cut
            self.wish_sale_last_cuts[sid] = prev
            # 手动拉列表后可稍长间隔；纯缓存查折扣时缩短，避免 28 人×12s 空等
            await asyncio.sleep(2.0 if cache_only else 8.0)

        stats["sale_count"] = len(all_sales)
        stats["quota_left"] = 0 if manual_one else self._wish_quota_left()
        try:
            self._wish_price_cache_save()
        except Exception:
            pass
        if only_sid:
            if only_sid in stats["unreadable"]:
                stats["status"] = "unreadable"
            elif only_sid in stats["empty"]:
                stats["status"] = "empty"
            elif not stats["priced_count"] and stats["wishlist_count"]:
                stats["status"] = "no_price_data"
            else:
                stats["status"] = "ok"
        else:
            if stats["unreadable"] and not stats["wishlist_count"]:
                stats["status"] = "unreadable"
            elif stats["empty"] and not stats["wishlist_count"]:
                stats["status"] = "empty"
            else:
                stats["status"] = "ok"

        if all_sales or new_sales:
            self._save_wish_sale_data()

        if new_sales:
            await self._push_wishlist_sales(new_sales, only_targets=only_targets)
            for e in new_sales:
                self.wish_sale_log.append({
                    "sid": e["sid"],
                    "player_name": e["player_name"],
                    "appid": e["appid"],
                    "name": e["name"],
                    "cut": e["cut"],
                    "price": e.get("current_price"),
                    "date": e["date"],
                })
            self.wish_sale_log = self.wish_sale_log[-200:]
            self._save_wish_sale_data()
        return new_sales, all_sales, stats

    def _format_sale_line(self, e: dict) -> str:
        """查价精简版文案：标题 + 最多 4 区现价（无史低/套餐）+ 商店链接。"""
        from ...presentation.renderers.game_detail import CURRENCY_SYMBOL
        from ...shared.utils.price import to_cny as _to_cny

        name = e.get("name") or e.get("appid")
        cut = int(e.get("cut") or 0)
        appid = e.get("appid")
        title = f"🎮《{name}》"
        if cut:
            title += f" -{cut}%"
        lines = [title]
        regions = e.get("region_prices") or {}
        if regions:
            for label, info in regions.items():
                info = info or {}
                price = info.get("current_price")
                if price is None:
                    lines.append(f"· {label}：未提供")
                    continue
                cur = str(info.get("currency") or "CNY")
                sym = info.get("symbol") or CURRENCY_SYMBOL.get(cur.upper(), "")
                try:
                    part = f"{sym}{float(price):g}"
                except (TypeError, ValueError):
                    part = f"{sym}{price}"
                cny = info.get("current_cny")
                if cny is not None and cur.upper() != "CNY":
                    try:
                        part += f"（¥{float(cny):.2f}）"
                    except (TypeError, ValueError):
                        pass
                if info.get("cut"):
                    part += f" -{int(info['cut'])}%"
                lines.append(f"· {label}：{part}")
        else:
            cur = str(e.get("currency") or "CNY")
            sym = CURRENCY_SYMBOL.get(cur.upper(), "¥" if cur.upper() == "CNY" else "")
            cur_p = e.get("current_price")
            reg_p = e.get("current_regular")
            if cur_p is not None:
                try:
                    part = f"{sym}{float(cur_p):g}"
                except (TypeError, ValueError):
                    part = f"{sym}{cur_p}"
                if reg_p is not None:
                    try:
                        if float(reg_p) > float(cur_p):
                            part += f"（原价 {sym}{float(reg_p):g}）"
                    except (TypeError, ValueError):
                        pass
                if cut:
                    part += f" -{cut}%"
                lines.append(f"· {self._wish_sale_country()}：{part}")
        sale_end_line = e.get("sale_end_line") or format_sale_end_line(
            e.get("sale_end_ts"), e.get("sale_end_text")
        )
        if sale_end_line:
            lines.append(f"· 折扣时限：{sale_end_line}")
        lines.append(f"https://store.steampowered.com/app/{appid}")
        return "\n".join(lines)

    async def _wish_download_cover_path(self, entry: dict):
        """优先下载商店横版封面；失败再退回方形图标。返回临时文件路径或 None。"""
        urls = []
        for u in (entry.get("cover_urls") or []):
            if u:
                urls.append(str(u))
        if entry.get("cover_url"):
            urls.append(str(entry["cover_url"]))
        if entry.get("header_image"):
            urls.append(str(entry["header_image"]))
        seen = set()
        uniq = []
        for u in urls:
            if u not in seen:
                seen.add(u)
                uniq.append(u)
        import tempfile
        proxies = []
        if getattr(self, "proxy", None):
            proxies.append(self.proxy)
        proxies.append(None)
        for url in uniq[:6]:
            for proxy in proxies:
                try:
                    async with shared_httpx_client(proxy=proxy, timeout=12, follow_redirects=False) as client:
                        resp = await client.get(url, headers={
                            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
                        })
                    if resp.status_code == 200 and resp.content and len(resp.content) > 800:
                        ctype = (resp.headers.get("content-type") or "").lower()
                        if ctype and "image" not in ctype and "octet-stream" not in ctype:
                            continue
                        with tempfile.NamedTemporaryFile(delete=False, suffix=".jpg") as tmp:
                            tmp.write(resp.content)
                            return tmp.name
                except Exception:
                    continue
        try:
            from ...presentation.renderers.game_lib import _fetch_icon
            img = await _fetch_icon(
                {
                    "appid": entry.get("appid"),
                    "name": entry.get("name"),
                    "cover_urls": entry.get("cover_urls"),
                    "cover_url": entry.get("cover_url"),
                },
                data_dir=self.data_dir,
                proxy=self.proxy,
            )
            if img is not None:
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="PNG")
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(buf.getvalue())
                    return tmp.name
        except Exception:
            pass
        return None

    def _bound_qqs_for_sid(self, sid) -> list:
        """返回绑定到该 SteamID 的 QQ 号列表（用于推送 @）。"""
        sid = str(sid or "")
        if not sid:
            return []
        out = []
        bind = getattr(self, "_bind_data", {}) or {}
        for qq, info in bind.items():
            qs = str(qq)
            if qs.startswith("__remark:"):
                continue
            if not isinstance(info, dict):
                continue
            sids = []
            if hasattr(self, "_bind_info_sids"):
                try:
                    sids = [str(x) for x in self._bind_info_sids(info)]
                except Exception:
                    sids = []
            if not sids:
                raw = info.get("sids") or ([info.get("sid")] if info.get("sid") else [])
                sids = [str(x) for x in raw]
            if sid in sids and qs not in out:
                out.append(qs)
        return out

    async def _push_wishlist_sales(self, sales, only_targets=None):
        """推送愿望单打折：按玩家拆分；有绑定 QQ 则先 @ 再合并转发。"""
        if not sales:
            return
        from astrbot.api.message_components import At, Node, Nodes
        import tempfile

        by_key = {}
        for e in sales:
            sid = str(e.get("sid") or "")
            targets = only_targets if only_targets is not None else self._wish_sale_push_targets(sid)
            for umo in targets or []:
                by_key.setdefault((umo, sid), []).append(e)

        if not by_key:
            logger.info("[wish_sale] 有折扣但无推送目标（请先 /game wish_sale on）")
            return

        bot_uin = "0"
        bot_name = "游戏监控"
        try:
            bot = getattr(self.context, "get_bot", lambda: None)()
            if bot:
                info = await bot.get_login_info()
                if isinstance(info, dict):
                    bot_name = str(info.get("nickname") or bot_name)
                    bot_uin = str(info.get("user_id") or bot_uin)
        except Exception:
            pass

        def _seg_from_path(path):
            if not path or not os.path.exists(path):
                return None
            try:
                return Image.fromFileSystem(path)
            except Exception:
                return None

        keys = sorted(by_key.keys(), key=lambda k: (str(k[0]), str(k[1])))
        sent_by_umo = {}
        region_hint = "、".join(self._wish_sale_regions())
        for umo, sid in keys:
            items = by_key[(umo, sid)]
            dedup, seen = [], set()
            for e in items:
                key = str(e.get("appid"))
                if key in seen:
                    continue
                seen.add(key)
                dedup.append(e)
            items = dedup
            if not items:
                continue

            player_name = items[0].get("player_name") or self._resolve_player_display_name(sid)
            bound_qqs = self._bound_qqs_for_sid(sid)
            covers = {}
            for e in items:
                covers[str(e.get("appid"))] = await self._wish_download_cover_path(e)

            header = (
                f"💝 {player_name} 的愿望单打折\n"
                f"共 {len(items)} 款 · 查价精简版（{region_hint}）"
            )
            # 先 @ 绑定 QQ，再发合并转发（合并转发里 At 往往不会触发提醒）
            if bound_qqs:
                try:
                    at_segs = [At(qq=str(qq)) for qq in bound_qqs]
                    await self.context.send_message(
                        umo,
                        MessageChain([
                            *at_segs,
                            Plain(f" {player_name} 愿望单有 {len(items)} 款折扣，请查收 ↓"),
                        ]),
                    )
                except Exception as e:
                    logger.warning(f"[wish_sale] @绑定QQ失败 sid={sid} qqs={bound_qqs}: {e}")

            try:
                nodes = [Node(uin=bot_uin or "0", name=bot_name, content=[Plain(header)])]
                for i, e in enumerate(items, 1):
                    content = [
                        Plain(f"📄 {player_name} · 第 {i}/{len(items)} 款"),
                        Plain(self._format_sale_line(e)),
                    ]
                    seg = _seg_from_path(covers.get(str(e.get("appid"))))
                    if seg is not None:
                        content.append(seg)
                    nodes.append(Node(uin=bot_uin or "0", name=bot_name, content=content))
                await self.context.send_message(umo, MessageChain([Nodes(nodes)]))
                names = "、".join(f"{e.get('name')}(-{e.get('cut')}%)" for e in items[:6])
                logger.info(f"[wish_sale] 推送 {player_name} {len(items)} 款 -> {umo} | {names}")
                sent_by_umo[umo] = sent_by_umo.get(umo, 0) + 1
                if sent_by_umo[umo] >= 1:
                    await asyncio.sleep(1.2)
            except Exception as ex:
                logger.error(f"[wish_sale] 推送失败 {player_name} -> {umo}: {ex}")
                try:
                    lines = [header]
                    for e in items[:6]:
                        lines.append(self._format_sale_line(e))
                    if len(items) > 6:
                        lines.append(f"…等 {len(items)} 款")
                    await self.context.send_message(umo, MessageChain().message("\n".join(lines)))
                except Exception as e2:
                    logger.error(f"[wish_sale] 文本回退也失败 {player_name}: {e2}")

    async def _game_wish_impl(self, event: AstrMessageEvent, target: str = ""):
        '''公开愿望单（全量分页，每页20条+封面）：/game wish @某人'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        from ...presentation.renderers.game_wishlist import (
            attach_wishlist_covers,
            render_wishlist_pages,
        )
        from ...presentation.renderers.game_coop import _extract_at_from_event, _extract_qq_list, _qq_to_steam_sid

        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        raw = ""
        for getter_name in ("get_message_str", "get_message"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    raw = str(getter() or "")
                    if raw:
                        break
                except Exception:
                    pass
        if not raw:
            raw = str(getattr(event, "message_str", "") or target or "")

        sid = None
        for part in re.findall(r"\b7656\d{13}\b", raw + " " + str(target or "")):
            if len(part) == 17:
                sid = part
                break
        if not sid:
            qq_list = []
            seen = set()
            for q in (_extract_at_from_event(event) + _extract_qq_list(raw) + _extract_qq_list(str(target or ""))):
                if q not in seen:
                    seen.add(q)
                    qq_list.append(q)
            for qq in qq_list:
                s = _qq_to_steam_sid(self, qq)
                if s:
                    sid = s
                    break
            if not sid and not qq_list:
                sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
                if len(sids) >= 1:
                    sid = sids[0]
        if not sid:
            yield event.plain_result("用法：/game wish @某人\n或 /game wish 7656xxxxxxxxxxx")
            return

        await self._price_ack(event, "正在读取公开愿望单并生成封面，请稍等...")
        items = await self.fetch_public_wishlist(sid)
        player_name = self._resolve_player_display_name(sid)
        cache_note = ""
        if items is None:
            # SSR 失败时回退本地缓存
            disk = self._load_wishlist_cache(sid)
            if disk and disk.get("items"):
                items = disk.get("items") or []
                age_min = int((time.time() - float(disk.get("fetched_at") or 0)) // 60)
                cache_note = f"\n（Steam 页面暂不可用，已用本地缓存 · 约 {age_min} 分钟前）"
            else:
                yield event.plain_result("未读到公开愿望单：资料非公开、愿望单隐藏，或 Steam 页面暂时不可用。")
                return
        if not items:
            yield event.plain_result(f"{player_name} 的公开愿望单为空（或无可见条目）。")
            return
        # 成功时写入本地缓存
        try:
            items = self._truncate_wishlist_items(items)
            self._save_wishlist_cache(sid, {
                "sid": sid,
                "player_name": player_name,
                "fetched_at": time.time(),
                "items": items,
                "item_appids": [str(x.get("appid")) for x in items if x.get("appid")],
                "count": len(items),
            })
            self._wish_sale_cache[sid] = {"ts": time.time(), "items": items}
        except Exception:
            pass

        try:
            items = await attach_wishlist_covers(items, data_dir=self.data_dir, proxy=self.proxy)
        except Exception as e:
            logger.warning(f"[wish] 封面加载失败: {e}")

        pages = render_wishlist_pages(
            player_name,
            items,
            font_path=self.get_font_path("NotoSansHans-Regular.otf"),
            page_size=20,
        )
        if not pages:
            yield event.plain_result(f"渲染失败，但已读到 {len(items)} 条公开愿望单数据。")
            return

        total_pages = len(pages)
        header = (
            f"💝 {player_name} 的愿望单 · 公开数据 {len(items)} 条 · 共 {total_pages} 页"
            f"{cache_note}\n"
            "说明：Steam 公开愿望单（游客可读）；可能少于客户端计数，成人向/下架可能未包含。"
        )
        import tempfile

        tmp_paths = []
        for png in pages:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(png)
                tmp_paths.append(tmp.name)

        # 1 页：普通消息即可；多页：合并转发（聊天记录），避免刷屏
        if total_pages == 1:
            try:
                yield event.chain_result([
                    Plain(header),
                    Image.fromFileSystem(tmp_paths[0]),
                ])
            except Exception as e:
                logger.warning(f"[wish] 单页发送失败: {e}")
                yield event.image_result(tmp_paths[0])
            return

        bot_uin = str(event.get_self_id() or "0")
        bot_name = "游戏监控"
        try:
            # 优先用机器人自身昵称，失败则用固定名
            sender_nick = str(getattr(getattr(event, "message_obj", None), "self_nickname", "") or "")
            if not sender_nick:
                bot = getattr(event, "bot", None)
                if bot is not None:
                    info = await bot.get_login_info()
                    if isinstance(info, dict):
                        sender_nick = str(info.get("nickname") or "")
            if sender_nick:
                bot_name = sender_nick
        except Exception:
            pass

        nodes = []
        for i, tmp_path in enumerate(tmp_paths):
            content = []
            if i == 0:
                content.append(Plain(header))
            content.append(Plain(f"📄 第 {i + 1}/{total_pages} 页"))
            content.append(Image.fromFileSystem(tmp_path))
            nodes.append(Node(uin=bot_uin or "0", name=bot_name, content=content))

        try:
            yield event.chain_result([Nodes(nodes)])
        except Exception as e:
            logger.warning(f"[wish] 合并转发失败，改为普通多图: {e}")
            try:
                chain_parts = [Plain(header)]
                for tmp_path in tmp_paths:
                    chain_parts.append(Image.fromFileSystem(tmp_path))
                yield event.chain_result(chain_parts)
            except Exception as e2:
                logger.warning(f"[wish] 多图单条发送失败，改为逐页: {e2}")
                for i, tmp_path in enumerate(tmp_paths):
                    try:
                        if i == 0:
                            yield event.chain_result([
                                Plain(header),
                                Image.fromFileSystem(tmp_path),
                            ])
                        else:
                            yield event.image_result(tmp_path)
                    except Exception as e3:
                        logger.warning(f"[wish] 第{i+1}页发送失败: {e3}")


    async def _game_wish_sale_impl(self, event: AstrMessageEvent, action: str = "status"):
        '''愿望单打折推送：on|off|status|test [@某人|SteamID]'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        raw = ""
        for getter_name in ("get_message_str", "get_message"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    raw = str(getter() or "")
                    if raw:
                        break
                except Exception:
                    pass
        action = str(action or "status").strip().lower()
        arg = ""
        m = re.search(r"(?:on|off|status|test|list|update|check|更新|查折扣|折扣)\s+(.+)$", raw, re.I)
        if m:
            arg = m.group(1).strip()
        elif len(str(action).split()) > 1:
            parts = action.split()
            action = parts[0]
            arg = " ".join(parts[1:])

        from ...presentation.renderers.game_coop import _extract_at_from_event, _extract_qq_list, _qq_to_steam_sid
        from ...shared.utils.notify_session import is_sendable_group_session

        def _resolve_sid_from_arg(text: str):
            text = str(text or "")
            for part in re.findall(r"\b7656\d{13}\b", text):
                if len(part) == 17:
                    return part
            qq_list = list(dict.fromkeys(_extract_at_from_event(event) + _extract_qq_list(text)))
            for qq in qq_list:
                s = _qq_to_steam_sid(self, qq)
                if s:
                    return str(s)
            return None

        if action in ("on", "开启", "启用"):
            self.wish_sale_enabled_groups.add(group_id)
            self._save_wish_sale_data()
            bound_n = len(self._wish_bound_steam_sids())
            yield event.plain_result(
                "已开启本群愿望单打折推送。\n"
                f"策略：凌晨 {self._wish_sale_night_hour()} 点自动拉列表+ITAD查折扣；"
                f"商店只补截止（夜间额度 {self._wish_sale_night_store_limit()}）\n"
                f"白天：update/check 手动；门槛 {self._wish_sale_min_cut()}%；"
                f"推送会 @ 有绑定的 QQ\n"
                f"wish_sale 群监控 SteamID 约 {len(self._wish_sale_sids())} 人；"
                f"绑定/监控缓存约 {bound_n} 人。\n"
                "指令：update / check / test / status / cache / list"
            )
            # 开启后：有缓存则立刻 ITAD 查一轮并推送当前折扣
            async def _boot():
                try:
                    await self._check_wishlist_sales(force_push_all=True, cache_only=True)
                except Exception:
                    logger.exception("[wish_sale] on 后缓存折扣检查失败")

            asyncio.create_task(_boot())
            return

        if action in ("update", "更新", "refresh", "拉取"):
            """手动从 Steam 更新愿望单列表（SSR），再按额度查一轮折扣。"""
            target_sid = _resolve_sid_from_arg(arg) if arg else None
            sids = [target_sid] if target_sid else self._wish_sale_sids()
            if not sids:
                yield event.plain_result("没有可更新的 SteamID（请先 wish_sale on 并确保群里有纯 SteamID）。")
                return
            yield event.plain_result(
                f"【1/2】开始拉愿望单列表：{len(sids)} 人\n"
                f"Steam SSR 中，每人之间约 3 秒；完成后自动 ITAD 查折扣并推送。"
            )
            ok_n = fail_n = 0
            detail = []
            total_items = 0
            n_sids = len(sids)
            step = max(1, n_sids // 3)  # 每 1/3 提醒一次
            for i, sid in enumerate(sids, 1):
                pname = self._resolve_player_display_name(sid)
                try:
                    items = await self._get_wishlist_for_sale(sid, force=True, cache_only=False)
                    if items is None:
                        fail_n += 1
                        detail.append(f"{sid[-4:]}:失败")
                        if i == 1 or i == n_sids or i % step == 0:
                            yield event.plain_result(
                                f"进度 {i}/{n_sids}（约 {i*100//n_sids}%）：{pname} 列表失败"
                            )
                    else:
                        ok_n += 1
                        n = len(items)
                        total_items += n
                        detail.append(f"{sid[-4:]}:{n}")
                        if i == 1 or i == n_sids or i % step == 0:
                            yield event.plain_result(
                                f"进度 {i}/{n_sids}（约 {i*100//n_sids}%）：{pname}（…{sid[-4:]}）"
                                f"愿望单 {n} 条已写入本地缓存"
                            )
                except Exception as e:
                    fail_n += 1
                    detail.append(f"{sid[-4:]}:{type(e).__name__}")
                    if i == 1 or i == n_sids or i % step == 0:
                        yield event.plain_result(
                            f"进度 {i}/{n_sids}：{pname} 异常 {type(e).__name__}"
                        )
                await asyncio.sleep(3)
            self._save_wish_sale_data()
            yield event.plain_result(
                f"【2/2】列表完成：成功 {ok_n}/失败 {fail_n}，合计缓存约 {total_items} 条\n"
                f"正在 ITAD 查折扣（会按进度提示，跳过未截止的有折扣款）…"
            )
            progress_lines: list = []

            async def _cb(msg: str):
                progress_lines.append(str(msg))

            async def _run_check():
                if target_sid:
                    return await self._check_wishlist_sales(
                        only_sid=target_sid,
                        cache_only=True,
                        force_push_all=True,
                        progress_cb=_cb,
                    )
                return await self._check_wishlist_sales(
                    cache_only=True,
                    force_push_all=True,
                    progress_cb=_cb,
                )

            check_task = asyncio.create_task(_run_check())
            while not check_task.done():
                while progress_lines:
                    yield event.plain_result(progress_lines.pop(0))
                await asyncio.sleep(1.0)
            while progress_lines:
                yield event.plain_result(progress_lines.pop(0))
            try:
                new_sales, all_sales, st = await check_task
            except Exception:
                new_sales, all_sales, st = [], [], {"status": "error"}
            mode = st.get("mode") or ("manual" if target_sid else "background")
            push_n = len(new_sales) or len(all_sales)
            sale_preview = []
            for e in (all_sales or [])[:6]:
                sale_preview.append(f"《{e.get('name')}》-{e.get('cut')}%")
            yield event.plain_result(
                f"更新+折扣检查完成\n"
                f"列表：{ok_n} 人成功 / {fail_n} 失败 · 缓存合计约 {total_items} 条\n"
                f"折扣：状态 {st.get('status')} · 实查 {st.get('scanned', 0)} · "
                f"跳过 {st.get('skipped', 0)} · 有折扣 {st.get('sale_count', 0)} 款\n"
                + (f"样例：{'；'.join(sale_preview)}\n" if sale_preview else "当前无 ≥ 门槛折扣\n")
                + f"已推送合并转发：{push_n} 款（有绑定 QQ 会先 @）\n"
                + (
                    f"单人模式 · 上限 {self._wish_sale_manual_limit()}（不占后台小时池）"
                    if mode == "manual"
                    else f"后台小时额度剩 {self._wish_quota_left()}/{self._wish_sale_hourly_limit()}"
                )
            )
            return

        if action in ("check", "查折扣", "折扣"):
            only_sid = _resolve_sid_from_arg(arg) if arg else None
            if not only_sid and self._wish_quota_left() <= 0:
                yield event.plain_result(
                    f"后台本小时额度已用完（{self._wish_sale_hourly_limit()}）。\n"
                    f"可指定单人立即查：/game wish_sale check @某人\n"
                    f"（单人一次最多 {self._wish_sale_manual_limit()} 条，不占后台小时池）"
                )
                return
            yield event.plain_result(
                f"正在查折扣（ITAD，会有进度提示）…"
                + (f" 目标：单人" if only_sid else " 目标：后台名单")
            )
            progress_lines = []

            async def _cb(msg: str):
                progress_lines.append(str(msg))

            check_task = asyncio.create_task(
                self._check_wishlist_sales(
                    only_sid=only_sid,
                    cache_only=True,
                    force_push_all=bool(only_sid),
                    progress_cb=_cb,
                )
            )
            while not check_task.done():
                while progress_lines:
                    yield event.plain_result(progress_lines.pop(0))
                await asyncio.sleep(1.0)
            while progress_lines:
                yield event.plain_result(progress_lines.pop(0))
            try:
                new_sales, all_sales, st = await check_task
            except Exception as e:
                yield event.plain_result(f"折扣检查失败：{e}")
                return
            mode = st.get("mode") or ("manual" if only_sid else "background")
            extra = (
                f"单人模式 scanned={st.get('scanned', 0)} / 上限 {self._wish_sale_manual_limit()}"
                if mode == "manual"
                else f"后台额度剩 {self._wish_quota_left()}/{self._wish_sale_hourly_limit()}"
            )
            push_note = ""
            if only_sid and all_sales:
                push_note = f"\n已推送合并转发 {len(all_sales)} 款（有绑定会 @）"
            elif only_sid:
                push_note = "\n无 ≥ 门槛的折扣，未推送"
            yield event.plain_result(
                f"折扣检查：{st.get('status')}（{mode}）\n"
                f"缓存愿望单合计 {st.get('wishlist_count', 0)} · 本轮商店/ITAD查询 {st.get('scanned', 0)} · "
                f"跳过 {st.get('skipped', 0)} · 有折扣 {st.get('sale_count', 0)}\n"
                f"{extra}{push_note}"
            )
            return

        if action in ("off", "关闭", "停用"):
            self.wish_sale_enabled_groups.discard(group_id)
            self._save_wish_sale_data()
            yield event.plain_result("已关闭本群愿望单打折推送（本地愿望单缓存仍会按间隔维护）。")
            return

        if action in ("cache", "缓存"):
            sids = self._wish_bound_steam_sids()
            ok = stale = miss = 0
            lines = [f"本地愿望单缓存（绑定+监控 {len(sids)} 人）"]
            ttl = self._wish_sale_interval_sec()
            samples = []
            for sid in sids:
                disk = self._load_wishlist_cache(sid)
                if not disk or disk.get("items") is None:
                    miss += 1
                    continue
                age = int(time.time() - float(disk.get("fetched_at") or 0))
                if age < ttl:
                    ok += 1
                else:
                    stale += 1
                if len(samples) < 8:
                    samples.append(
                        f"  {disk.get('player_name') or sid}: {disk.get('count', len(disk.get('items') or []))} 条 · {format_cache_age(age)}前"
                    )
            lines.append(f"新鲜 {ok} · 过期 {stale} · 尚无缓存 {miss}")
            lines.append(
                f"列表策略：凌晨 {self._wish_sale_night_hour()} 点自动拉 / 或手动 update；"
                f"折扣用 ITAD + 本地缓存"
            )
            lines.extend(samples)
            ban = float(getattr(self, "_wish_store_ban_until", 0) or 0)
            if ban and time.time() < ban:
                lines.append(f"⚠ Steam SSR 冷却中，剩余约 {format_cache_age(ban - time.time())}")
            yield event.plain_result("\n".join(lines))
            return

        if action in ("test", "测试", "查"):
            sid = _resolve_sid_from_arg(arg)
            if not sid:
                # 默认：本群第一个监控的 SteamID
                sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
                sid = sids[0] if sids else None
            if not sid:
                yield event.plain_result(
                    "未找到可检查的 Steam 玩家。\n"
                    "用法：/game wish_sale test @某人\n或 /game wish_sale test 7656xxxxxxxxxxx"
                )
                return
            yield event.plain_result(f"正在检查 {sid} 的愿望单折扣（优先本地缓存）...")
            # 内存短缓存清掉；磁盘缓存保留，SSR 失败时会回退
            self._wish_sale_cache.pop(sid, None)
            if self.steam_guard_active():
                yield event.plain_result(self._steam_guard_block_msg() or "Steam 资料扫描中，请稍后再试。")
                return
            targets = [event.unified_msg_origin] if getattr(event, "unified_msg_origin", None) else None
            if targets:
                targets = [t for t in targets if is_sendable_group_session(t)] or None
            new_sales, all_sales, stats = await self._check_wishlist_sales(
                force_push_all=True,
                only_sid=sid,
                only_targets=targets,
                cache_only=True,
            )
            player_name = self._resolve_player_display_name(sid)
            country = self._wish_sale_country()
            status = (stats or {}).get("status")
            disk = self._load_wishlist_cache(sid)
            cache_info = ""
            if disk and disk.get("items") is not None:
                age = int(time.time() - float(disk.get("fetched_at") or 0))
                cache_info = f"\n本地缓存：{disk.get('count', len(disk.get('items') or []))} 条 · {age//60} 分钟前"
            # 持久化冷却状态
            try:
                if getattr(self, "_wish_store_ban_until", 0):
                    self._save_wish_sale_data()
            except Exception:
                pass
            if status == "cooldown" or (
                getattr(self, "_wish_store_ban_until", 0)
                and time.time() < float(self._wish_store_ban_until)
            ):
                left = max(0, int(float(getattr(self, "_wish_store_ban_until", 0) or 0) - time.time()))
                stale_note = ""
                if disk and disk.get("items"):
                    stale_note = f"\n已有本地缓存 {disk.get('count', 0)} 条，冷却结束后会用缓存查折扣/下次到期再刷 Steam。"
                yield event.plain_result(
                    f"⏸ Steam 愿望单接口正在冷却（约 {left // 60} 分钟后自动恢复）。"
                    f"{stale_note}\n"
                    "原因：短时间对 store 愿望单页请求过多，出口 IP 被 403 风控。\n"
                    "请稍后再试；后台会按间隔自动重试，勿连续 test。"
                )
            elif status == "unreadable":
                if disk and disk.get("items"):
                    yield event.plain_result(
                        f"Steam 愿望单暂不可读，但本地有缓存 {disk.get('count', 0)} 条"
                        f"（{int(time.time() - float(disk.get('fetched_at') or 0))//60} 分钟前）。{cache_info}\n"
                        f"本轮未能用缓存完成折扣扫描的话，等 SSR 恢复后再试。"
                    )
                else:
                    yield event.plain_result(
                        f"❌ 未能读取 {player_name}（{sid}）的公开愿望单，且本地无缓存。\n"
                        "可能原因：愿望单非公开 / Steam 风控（HTTP 403）/ 网络异常。\n"
                        "可稍后再试，或让对方确认愿望单为公开。"
                    )
            elif status == "empty":
                yield event.plain_result(f"{player_name} 的公开愿望单为空（或无可见条目）。{cache_info}")
            elif status == "no_price_data":
                yield event.plain_result(
                    f"读到了 {player_name} 愿望单 {stats.get('wishlist_count', 0)} 条，"
                    f"但区服 {country} 未取到价格数据，无法判断折扣。{cache_info}"
                )
            elif not all_sales:
                yield event.plain_result(
                    f"{player_name} 愿望单检查完成：共 {stats.get('wishlist_count', 0)} 条，"
                    f"{stats.get('priced_count', 0)} 条有价格，当前区服 {country} 无折扣。{cache_info}"
                )
            elif not new_sales:
                lines = [f"{player_name} 愿望单折扣 {len(all_sales)} 款：{cache_info}"]
                for e in all_sales[:8]:
                    lines.append(self._format_sale_line(e))
                yield event.plain_result("\n".join(lines))
            # 有折扣时已由 _push_wishlist_sales 按人推送聊天记录
            return

        if action in ("list", "日志", "log"):
            logs = list(self.wish_sale_log)[-10:]
            if not logs:
                yield event.plain_result("暂无愿望单打折推送记录。")
                return
            lines = ["最近愿望单打折推送："]
            for e in reversed(logs):
                lines.append(f"{e.get('date')} {e.get('player_name')} 《{e.get('name')}》 -{e.get('cut')}%")
            yield event.plain_result("\n".join(lines))
            return

        # status
        enabled = group_id in self.wish_sale_enabled_groups
        sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
        bound_n = len(self._wish_bound_steam_sids())
        cached_n = 0
        for sid in self._wish_bound_steam_sids():
            disk = self._load_wishlist_cache(sid)
            if disk and disk.get("items") is not None:
                cached_n += 1
        state = "已开启" if enabled else "已关闭"
        ban = float(getattr(self, "_wish_store_ban_until", 0) or 0)
        ban_line = ""
        if ban and time.time() < ban:
            ban_line = f"\n⚠ Steam 商店/SSR 冷却中，剩余约 {int(ban - time.time())//60} 分钟"
        pos_map = getattr(self, "wish_sale_scan_pos", {}) or {}
        pos_preview = ", ".join(f"{k[-4:]}:{v}" for k, v in list(pos_map.items())[:6])
        pc = getattr(self, "_wish_sale_price_cache", {}) or {}
        night_h = self._wish_sale_night_hour()
        night_done = getattr(self, "_wish_sale_night_date", "") or "（今日未跑）"
        last_logs = list(self.wish_sale_log or [])[-3:]
        log_lines = []
        for e in reversed(last_logs):
            log_lines.append(f"  {e.get('date')} {e.get('player_name')}《{e.get('name')}》-{e.get('cut')}%")
        q_used = self._wish_quota_state().get("used", 0)
        yield event.plain_result(
            f"══ 愿望单折扣 · status ══\n"
            f"本群推送：{state}（{group_id}）\n"
            f"监控 SteamID：{len(sids)} 人 · 绑定/监控缓存覆盖 {cached_n}/{bound_n} 人\n"
            f"── 策略 ──\n"
            f"· 列表：凌晨 {night_h} 点自动 SSR"
            f"{'（今日 '+night_done+'）' if night_h >= 0 else '（已关）'}\n"
            f"· 判折扣：ITAD；商店只补截止（夜间额度 {self._wish_sale_night_store_limit()}）\n"
            f"· 手动：update 拉列表并推送当前折扣 · check/test @某人\n"
            f"· 门槛 ≥{self._wish_sale_min_cut()}% · 区服 {self._wish_sale_country()}\n"
            f"── 额度/缓存 ──\n"
            f"· 后台小时商店额度 {q_used}/{self._wish_sale_hourly_limit()}\n"
            f"· 单人手动上限 {self._wish_sale_manual_limit()} · 无折扣复查 {self._wish_sale_recheck_hours()}h\n"
            f"· 价格缓存条目 {len(pc)} · 折扣记录玩家 {len(self.wish_sale_last_cuts or {})} 人\n"
            + (f"· 扫描游标 {pos_preview}\n" if pos_preview else "")
            + ban_line + "\n"
            + ("── 最近推送 ──\n" + "\n".join(log_lines) if log_lines else "── 最近推送 ──\n  （暂无）")
            + "\n指令：on|off|update|check|test|status|cache|list"
        )

