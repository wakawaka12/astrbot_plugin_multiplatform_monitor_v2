"""全成就扫描 / 卡片缓存应用服务（从主插件拆出）。

运行时由 SteamStatusMonitorV3 多继承挂载；本文件不注册 AstrBot 指令。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import time
import traceback
import hashlib

from astrbot.api.event import AstrMessageEvent
from astrbot.api.event import filter

from ...shared.logging import logger
from ...shared.utils.cache_age import format_cache_age


class PerfectGamesServiceMixin:
    """全成就全库扫描、卡片缓存与指令实现。"""

    def _perfect_card_dir(self, sid: str) -> str:
        path = os.path.join(self.data_dir, "perfect_card_cache", str(sid or "unknown"))
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
        return path

    @staticmethod
    def _perfect_games_signature(games) -> str:
        import hashlib
        parts = []
        for g in games or []:
            parts.append(
                f"{g.get('appid')}:{int(g.get('achievement_count') or 0)}:{int(g.get('playtime_minutes') or 0)}"
            )
        return hashlib.md5("|".join(parts).encode("utf-8")).hexdigest()

    def _load_perfect_card_cache(self, sid: str, signature: str):
        """签名一致时返回 (page_paths, named_games, meta)；否则 None。"""
        cdir = self._perfect_card_dir(sid)
        meta_path = os.path.join(cdir, "meta.json")
        if not os.path.isfile(meta_path):
            return None
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            return None
        if not meta or signature != "any" and meta.get("signature") != signature:
            return None
        page_files = meta.get("page_files") or []
        pages = []
        for fn in page_files:
            fp = os.path.join(cdir, str(fn))
            if not os.path.isfile(fp):
                return None
            pages.append(fp)
        if not pages:
            return None
        return pages, meta.get("named_games") or [], meta

    def _save_perfect_card_cache(self, sid: str, signature: str, page_pngs, named, meta_extra=None):
        cdir = self._perfect_card_dir(sid)
        try:
            for fn in os.listdir(cdir):
                if fn.startswith("page_") and fn.endswith(".png"):
                    try:
                        os.remove(os.path.join(cdir, fn))
                    except OSError:
                        pass
        except OSError:
            pass
        page_files = []
        for i, png in enumerate(page_pngs or [], 1):
            fn = f"page_{i:02d}.png"
            fp = os.path.join(cdir, fn)
            try:
                with open(fp, "wb") as f:
                    f.write(png)
                page_files.append(fn)
            except OSError as e:
                logger.warning(f"[perfect_cache] 写页失败 {fp}: {e}")
        meta = {
            "signature": signature,
            "page_files": page_files,
            "named_games": named,
            "count": len(named or []),
            "ts": time.time(),
        }
        if meta_extra:
            meta.update(meta_extra)
        try:
            with open(os.path.join(cdir, "meta.json"), "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False)
        except OSError as e:
            logger.warning(f"[perfect_cache] 写 meta 失败: {e}")
        return [os.path.join(cdir, fn) for fn in page_files]


    @staticmethod
    def _owned_library_ids_and_sig(owned_games):
        """已购库 appid 集合 + 签名（用于判断库存是否变化）。"""
        import hashlib
        ids = []
        for g in owned_games or []:
            aid = str(g.get("appid") or "").strip()
            if aid.isdigit():
                ids.append(aid)
        uniq = sorted(set(ids))
        sig = hashlib.md5(",".join(uniq).encode("utf-8")).hexdigest()
        return set(uniq), sig

    async def _game_ach_impl(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
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
            raw = str(getattr(event, "message_str", "") or f"{arg1} {arg2}")
        raw = re.sub(r"^[/.。／]*\s*(?:game\s+)?ach\s*", "", raw, flags=re.I).strip()
        if arg1 or arg2:
            merged = " ".join(x for x in (str(arg1 or "").strip(), str(arg2 or "").strip()) if x)
            if merged:
                # 参数优先，避免引用消息里的历史文本干扰
                raw = merged
        raw = raw.strip()
        logger.info(f"[game_ach] 入参 arg1={arg1!r} arg2={arg2!r} raw={raw!r}")

        # —— 手动停止：仅当指令本身就是 stop/cancel/中止（不要用全文正则，避免引用消息误伤）——
        cmd_tokens = re.findall(r"[A-Za-z]+|[一-鿿]+", raw)
        stop_word = None
        for t in cmd_tokens:
            tl = t.lower()
            if tl in ("stop", "cancel", "abort", "停止", "中止", "手动停"):
                stop_word = t
                break
        if stop_word and not re.search(r"7656\d{13}|\d{7,}", raw.replace(stop_word, "")):
            st = getattr(self, "_steam_scan_state", None)
            if not st or not self.steam_guard_active():
                yield event.plain_result("当前没有进行中的全成就扫描。")
                return
            try:
                st["cancel"].set()
            except Exception:
                pass
            yield event.plain_result(
                f"🛑 已请求停止：{st.get('label') or '全成就扫描'}\n"
                f"扫描将尽快结束并释放接口锁，随后其它功能恢复。"
            )
            return

        # 一进来就查锁：扫描中一律拒绝
        if self.steam_guard_active():
            g = getattr(self, "_steam_api_guard", None) or {}
            label = g.get("label") or "Steam 资料扫描"
            elapsed = int(time.time() - float(g.get("ts") or 0))
            yield event.plain_result(
                f"⛔ 已有任务进行中：{label}（约 {elapsed} 秒）\n"
                f"扫描结束前，/game ach 与 /game ach @某人 fresh 均不可用，避免叠扫触发风控。\n"
                f"停止当前扫描：/game ach stop\n"
                f"状态轮询不受影响。"
            )
            return
        try:
            from ...presentation.renderers.achievement_list import render_achievement_list_image
            from ...presentation.renderers.perfect_games import (
                render_perfect_games_image,
                render_perfect_games_pages,
            )
        except Exception as e:
            logger.exception(f"[game_ach] 渲染模块导入失败: {e}")
            yield event.plain_result(f"成就模块加载失败：{e}")
            return

        steamid = None
        appid = None
        text = raw
        m_at = re.search(r"\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)", text)
        if m_at:
            qq = m_at.group(1) or m_at.group(2) or m_at.group(3) or m_at.group(4)
            try:
                steamid = self._primary_steam_sid_for_qq(qq) or None
            except Exception as e:
                logger.warning(f"[game_ach] 解析QQ绑定失败 qq={qq}: {e}")
                steamid = None
            text = (text[:m_at.start()] + " " + text[m_at.end():]).strip()
        for part in re.findall(r"\d+", text):
            if len(part) == 17 and part.startswith("7656"):
                steamid = steamid or part
            elif appid is None and part.isdigit() and 10 <= int(part) <= 40_000_000:
                if not (5 <= len(part) <= 12 and not steamid and m_at):
                    appid = int(part)

        def _pick_sid():
            sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
            return sids[0] if sids else None

        # —— 模式 A：全成就游戏列表 ——
        if appid is None:
            if not steamid:
                steamid = _pick_sid()
            if not steamid:
                yield event.plain_result(
                    "用法：/game ach @某人\n"
                    "全库扫描该玩家达成全部成就（100%）的游戏，合并转发多图。\n"
                    "强制重扫：/game ach @某人 fresh\n"
                    "停止扫描：/game ach stop\n"
                    "查单款游戏成就：/game ach <appid> @某人"
                )
                return
            if not self.API_KEY:
                yield event.plain_result("未配置 Steam API Key，无法查询成就。")
                return
            blk = self._steam_guard_block_msg()
            if blk:
                yield event.plain_result(
                    "⛔ 扫描进行中，已取消本次 /game ach（含 fresh）。\n" + blk
                )
                return
            force = bool(re.search(r"(?i)\b(fresh|重扫|全量|force)\b", raw))
            bind_qq = None
            if m_at:
                bind_qq = m_at.group(1) or m_at.group(2) or m_at.group(3) or m_at.group(4)
            sid_candidates = []
            if bind_qq:
                try:
                    sid_candidates = self._all_steam_sids_for_qq(bind_qq)
                except Exception:
                    sid_candidates = []
            if steamid not in sid_candidates:
                sid_candidates = [steamid]

            player_name = self._resolve_player_display_name(steamid)
            data = None
            used_sid = steamid
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            cancel_ev = asyncio.Event()
            self._begin_steam_guard(
                f"全库扫描 {player_name} 的全成就游戏"
                + ("（fresh 强制重扫）" if force else ""),
                umo=umo,
            )
            st = self._steam_scan_state or {}
            st["cancel"] = cancel_ev
            last_prog = {"pct": 0}

            async def _progress_cb(attempted, total, perfect_n, pct):
                # 进度通知：约每 1/3 一次（33%）
                if pct < last_prog["pct"] + 33 and attempted < total:
                    return
                last_prog["pct"] = (pct // 33) * 33
                if not umo:
                    return
                try:
                    await self.context.send_message(
                        umo,
                        MessageChain().message(
                            f"⏳ 全成就扫描进度 {last_prog['pct'] or pct}%"
                            f"（已处理 {attempted}/{total}）"
                            f" · 已确认全成就 {perfect_n} 款\n"
                            f"停止：/game ach stop"
                        ),
                    )
                except Exception as e:
                    logger.debug(f"[perfect] 进度消息发送失败: {e}")

            try:
                for cand in sid_candidates:
                    if cancel_ev.is_set():
                        break
                    cache_path = os.path.join(self.data_dir, f"perfect_games_{cand}.json")
                    cache = None
                    if not force and os.path.isfile(cache_path):
                        try:
                            with open(cache_path, encoding="utf-8") as f:
                                cache = json.load(f)
                        except Exception:
                            cache = None
                    try:
                        conc = int((self.config or {}).get("perfect_scan_concurrency", 10) or 10)
                    except (TypeError, ValueError):
                        conc = 10
                    conc = max(2, min(24, conc))

                    # 读当前库存（与购游戏通知同一数据源）
                    try:
                        owned = await self.fetch_owned_games(cand)
                    except Exception as e:
                        logger.error(f"[perfect] 读取游戏库失败 sid={cand}: {e}")
                        owned = None
                    if owned is None and cache is None:
                        yield event.plain_result(
                            f"读取 {cand} 游戏库失败（隐私或 API 异常），跳过该绑定。"
                        )
                        continue
                    owned = owned or []
                    cur_ids, cur_sig = self._owned_library_ids_and_sig(owned)
                    player_cand = self._resolve_player_display_name(cand)

                    # —— 联动：无新购 → 用旧库存/旧扫描；有新购 → 只扫新增；fresh → 全量 ——
                    if (not force) and cache and cache.get("games") is not None:
                        old_ids = set(str(x) for x in (cache.get("owned_appids") or []))
                        old_sig = cache.get("owned_sig") or ""
                        cache_ts = float(cache.get("ts") or 0)
                        cache_age = time.time() - cache_ts if cache_ts else None
                        age_txt = format_cache_age(cache_age)
                        # 旧缓存没有 sig 时：退回 TTL（默认 12h）
                        ttl = 12 * 3600
                        cache_age_ok = cache_ts and (time.time() - cache_ts) < ttl
                        # 即使库存未变，超过 7 天也提示数据偏旧（不自动全扫，除非 fresh）
                        stale_note = ""
                        if cache_age and cache_age > 7 * 86400:
                            stale_note = "\n⚠ 缓存已超过 7 天，如需最新成就请用 fresh"
                        if old_sig and old_sig == cur_sig:
                            d0 = cache
                            yield event.plain_result(
                                f"📦 库存未变化，复用上次扫描：{player_cand}（{cand}）\n"
                                f"全成就 {len(cache.get('games') or [])} 款 · 库存 {len(cur_ids)} 款 · "
                                f"扫描缓存年龄：{age_txt}{stale_note}\n"
                                f"强制全库重扫：/game ach @某人 fresh"
                            )
                        elif old_ids and old_sig:
                            added = sorted(cur_ids - old_ids)
                            removed = sorted(old_ids - cur_ids)
                            kept_ids = cur_ids
                            old_games = [
                                g for g in (cache.get("games") or [])
                                if str(g.get("appid")) in kept_ids
                            ]
                            if not added:
                                # 只减少了库存：过滤后直接复用
                                d0 = dict(cache)
                                d0["games"] = old_games
                                d0["owned_appids"] = sorted(cur_ids)
                                d0["owned_sig"] = cur_sig
                                d0["owned_count"] = len(owned)
                                d0["ts"] = time.time()
                                d0["source"] = "cache_removed_only"
                                try:
                                    with open(cache_path, "w", encoding="utf-8") as f:
                                        json.dump(d0, f, ensure_ascii=False)
                                except Exception:
                                    pass
                                yield event.plain_result(
                                    f"📦 库存有移除无新增，已复用旧扫描：{player_cand}（{cand}）\n"
                                    f"全成就 {len(old_games)} 款（已剔除不在库中的条目）\n"
                                    f"强制全库重扫：/game ach @某人 fresh"
                                )
                            else:
                                # 有新购：只扫描新增 appid，合并旧结果
                                yield event.plain_result(
                                    f"🛒 检测到库存变化：{player_cand}（{cand}）\n"
                                    f"新增 {len(added)} 款 / 移除 {len(removed)} 款 · "
                                    f"仅扫描新增条目的成就，其余沿用旧结果…\n"
                                    f"停止：/game ach stop"
                                )
                                added_games = [g for g in owned if str(g.get("appid")) in set(added)]
                                new_part = await self.fetch_perfect_games(
                                    cand,
                                    api_key=self.API_KEY,
                                    max_check=0,
                                    concurrency=conc,
                                    owned_games=added_games,
                                    only_appids=added,
                                    cancel_event=cancel_ev,
                                    progress_cb=_progress_cb,
                                    progress_step=0.33,
                                )
                                if cancel_ev.is_set() or (new_part or {}).get("cancelled"):
                                    yield event.plain_result("🛑 增量扫描已停止，未更新成就缓存。")
                                    return
                                new_perfect = (new_part or {}).get("games") or []
                                merged = old_games + [
                                    g for g in new_perfect
                                    if str(g.get("appid")) not in {str(x.get("appid")) for x in old_games}
                                ]
                                merged.sort(key=lambda x: int(x.get("playtime_minutes") or 0), reverse=True)
                                d0 = {
                                    "steamid": cand,
                                    "owned_count": len(owned),
                                    "checked": int(cache.get("checked") or 0) + int((new_part or {}).get("checked") or 0),
                                    "attempted": int((new_part or {}).get("attempted") or 0),
                                    "games": merged,
                                    "perfect_count_hint": (new_part or {}).get("perfect_count_hint") or cache.get("perfect_count_hint"),
                                    "cancelled": False,
                                    "owned_appids": sorted(cur_ids),
                                    "owned_sig": cur_sig,
                                    "ts": time.time(),
                                    "source": "incremental_on_purchase",
                                    "last_added_appids": added[:50],
                                }
                                try:
                                    os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                                    with open(cache_path, "w", encoding="utf-8") as f:
                                        json.dump(d0, f, ensure_ascii=False)
                                except Exception as e:
                                    logger.warning(f"[perfect] 增量缓存写入失败: {e}")
                                yield event.plain_result(
                                    f"✅ 增量完成：新增扫描 {len(added)} 款，"
                                    f"其中全成就 {len(new_perfect)} 款；当前合计全成就 {len(merged)} 款"
                                )
                        elif cache_age_ok and not old_sig:
                            # 旧格式缓存：TTL 内仍复用，但标明年龄
                            d0 = cache
                            yield event.plain_result(
                                f"📦 使用本地缓存：{player_cand}（{cand}）全成就 {len(d0.get('games') or [])} 款\n"
                                f"扫描缓存年龄：{age_txt}{stale_note}\n"
                                f"如需强制重扫：/game ach @某人 fresh"
                            )
                        else:
                            d0 = None
                    else:
                        d0 = None

                    if d0 is None:
                        # 全量扫描
                        if not owned and len(sid_candidates) > 1:
                            logger.info(f"[perfect] sid={cand} 库为空，尝试下一个绑定")
                            continue
                        owned_n = len(owned)
                        if cand == steamid or data is None:
                            est_sec = int(max(8, (owned_n / max(conc, 1)) * 0.35 + 8))
                            est_txt = f"约 {est_sec // 60} 分 {est_sec % 60} 秒" if est_sec >= 60 else f"约 {est_sec} 秒"
                            extra_bind = ""
                            if len(sid_candidates) > 1:
                                extra_bind = f"\n该 QQ 绑定了 {len(sid_candidates)} 个 SteamID，已优先使用：{player_cand}（{cand}）"
                            yield event.plain_result(
                                f"📚 {player_cand} 游戏库共 {owned_n} 款{extra_bind}\n"
                                f"将全库扫描全成就 · 并发 {conc} · 预计耗时 {est_txt}\n"
                                f"进度约每 1/3 提示一次 · 停止：/game ach stop\n"
                                f"扫描期间其它 Steam 查询暂不可用（防风控）…"
                            )
                        try:
                            d0 = await self.fetch_perfect_games(
                                cand,
                                api_key=self.API_KEY,
                                max_check=0,
                                concurrency=conc,
                                owned_games=owned,
                                cancel_event=cancel_ev,
                                progress_cb=_progress_cb,
                                progress_step=0.33,
                            )
                        except Exception as e:
                            logger.error(f"[perfect] 查询失败 sid={cand}: {e}")
                            yield event.plain_result(f"全成就扫描异常：{e}")
                            d0 = None
                        if d0 is None:
                            continue
                        if d0.get("cancelled"):
                            yield event.plain_result(
                                f"🛑 已停止扫描 {player_cand}（{cand}）\n"
                                f"已处理约 {d0.get('attempted')}/{d0.get('owned_count')} · "
                                f"成功核对 {d0.get('checked')} · 临时确认全成就 {len(d0.get('games') or [])} 款\n"
                                f"结果不完整，未写入缓存。接口锁已释放。"
                            )
                            data = d0
                            used_sid = cand
                            break
                        d0["owned_appids"] = sorted(cur_ids)
                        d0["owned_sig"] = cur_sig
                        d0["ts"] = time.time()
                        d0["source"] = "full_scan"
                        try:
                            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                            with open(cache_path, "w", encoding="utf-8") as f:
                                json.dump(d0, f, ensure_ascii=False)
                        except Exception as e:
                            logger.warning(f"[perfect] 缓存写入失败: {e}")
                    if d0 and (d0.get("games") or int(d0.get("checked") or 0) > 0):
                        data = d0
                        used_sid = cand
                        break
                    if d0 and data is None:
                        data = d0
                        used_sid = cand
            finally:
                self._end_steam_guard()
            steamid = used_sid
            player_name = self._resolve_player_display_name(steamid)
            if data is None:
                yield event.plain_result("获取游戏库失败：资料可能为隐私，或 Steam API 异常。")
                return
            if data.get("cancelled"):
                return

            games = data.get("games") or []
            if not games:
                hint = data.get("perfect_count_hint")
                extra = f"（社区展示 {hint} 款）" if hint is not None else ""
                yield event.plain_result(
                    f"{player_name} 未扫描到全成就游戏{extra}。\n"
                    f"已检查 {data.get('checked')}/{data.get('owned_count')} 款。\n"
                    "隐私库、无成就游戏或成就 API 受限时可能查不到。"
                )
                return

            signature = self._perfect_games_signature(games)
            # 1) 本地卡片缓存：无新全成就时直接发旧图
            cached = None if force else self._load_perfect_card_cache(steamid, signature)
            if cached:
                page_paths, named, cmeta = cached
                yield event.plain_result(
                    f"📦 命中本地卡片缓存：{player_name} · 全成就 {len(games)} 款\n"
                    f"与上次扫描结果一致，直接发送已渲染图片（{len(page_paths)} 页）。"
                )
                pages = page_paths
            else:
                # 2) 全量渲染（不再截断 36 款）；译名写入缓存便于下次复用
                named = []
                prev_names = {}
                try:
                    old_meta = self._load_perfect_card_cache(steamid, "any")  # 不同签名，只拿不到
                except Exception:
                    old_meta = None
                # 若仅顺序/时长微调但 appid 集合相同，仍会重绘；签名已含时长
                yield event.plain_result(
                    f"🖼 开始渲染 {len(games)} 款全成就卡片…\n"
                    f"流程：译名 → 封面 → 分页成图；进度约每 1/3 提示一次。\n"
                    f"完成后写入本地缓存，下次无变化将直接发旧图。\n"
                    f"停止：/game ach stop"
                )
                render_total = len(games)
                name_state = {"last": 0}
                for idx, g in enumerate(games, 1):
                    g = dict(g)
                    try:
                        zh = await self.get_chinese_game_name(g.get("appid"), g.get("name"))
                        if zh:
                            g["name"] = zh
                    except Exception:
                        pass
                    named.append(g)
                    if cancel_ev.is_set():
                        yield event.plain_result("🛑 渲染阶段已停止，未写入卡片缓存。")
                        return
                    pct = int(idx * 100 / max(render_total, 1))
                    # 译名进度：约每 1/3 一次
                    if pct >= name_state["last"] + 33 or idx == render_total:
                        name_state["last"] = (pct // 33) * 33
                        if umo:
                            try:
                                await self.context.send_message(
                                    umo,
                                    MessageChain().message(
                                        f"⏳ 出图进度 · 译名 {name_state['last'] or pct}%（{idx}/{render_total}）\n"
                                        f"停止：/game ach stop"
                                    ),
                                )
                            except Exception:
                                pass
                logger.info(f"[perfect] 开始渲染卡片 {player_name} 全量={len(named)}")
                card_state = {"icons_last": 0, "page_last": 0}

                async def _render_prog(phase, done, tot, pct):
                    if not umo:
                        return
                    try:
                        if phase == "icons":
                            # 封面：约每 1/3 一次
                            if pct < card_state["icons_last"] + 33 and done < tot:
                                return
                            card_state["icons_last"] = (pct // 33) * 33
                            msg = f"⏳ 出图进度 · 封面 {card_state['icons_last'] or pct}%（{done}/{tot}）"
                        else:
                            # 分页：约每 1/3 页一次 + 最后一页
                            step = max(1, tot // 3)
                            if done < tot and done < card_state["page_last"] + step:
                                return
                            card_state["page_last"] = done
                            msg = f"⏳ 出图进度 · 分页 {done}/{tot} 页"
                        await self.context.send_message(
                            umo,
                            MessageChain().message(f"{msg}\n停止：/game ach stop"),
                        )
                    except Exception:
                        pass

                try:
                    page_pngs = await render_perfect_games_pages(
                        player_name,
                        named,
                        font_path=self.get_font_path("NotoSansHans-Regular.otf"),
                        data_dir=self.data_dir,
                        proxy=self.proxy,
                        checked=int(data.get("checked") or 0),
                        owned_count=int(data.get("owned_count") or 0),
                        perfect_hint=data.get("perfect_count_hint"),
                        page_size=12,
                        progress_cb=_render_prog,
                        cancel_event=cancel_ev,
                    )
                except Exception as e:
                    logger.error(f"[perfect] 分页渲染失败: {e}")
                    page_pngs = []
                logger.info(f"[perfect] 渲染完成 pages={len(page_pngs)}")
                if cancel_ev.is_set():
                    yield event.plain_result("🛑 渲染阶段已停止，未写入卡片缓存。")
                    return
                if not page_pngs:
                    lines = [f"{player_name} · 全成就游戏 {len(named)} 款（渲染失败）"]
                    for i, g in enumerate(named[:15], 1):
                        lines.append(
                            f"{i}. 《{g.get('name')}》 {g.get('achievement_count')}成就"
                        )
                    yield event.plain_result("\n".join(lines))
                    return
                pages = self._save_perfect_card_cache(
                    steamid,
                    signature,
                    page_pngs,
                    named,
                    meta_extra={
                        "player_name": player_name,
                        "checked": data.get("checked"),
                        "owned_count": data.get("owned_count"),
                        "perfect_count_hint": data.get("perfect_count_hint"),
                    },
                )
                yield event.plain_result(
                    f"✅ 已渲染并缓存：{len(games)} 款 · {len(pages)} 页卡片\n"
                    f"下次扫描无新增全成就时将直接复用本地图。"
                )

            from astrbot.api.message_components import Node, Nodes, Image as ImgComp, Plain
            import tempfile
            bot_uin = "0"
            bot_name = "游戏监控"
            try:
                bot = getattr(event, "bot", None)
                if bot:
                    info = await bot.get_login_info()
                    if isinstance(info, dict):
                        bot_name = str(info.get("nickname") or bot_name)
                        bot_uin = str(info.get("user_id") or bot_uin)
            except Exception:
                pass
            header = (
                f"🏆 {player_name} 的全成就游戏\n"
                f"共 {len(games)} 款 · 成功核对 {data.get('checked')}/{data.get('owned_count')} 款"
                + (f" · 社区展示 {data.get('perfect_count_hint')} 款" if data.get("perfect_count_hint") is not None else "")
                + ("\n（本地缓存卡片）" if cached else "")
            )
            nodes = [Node(uin=bot_uin or "0", name=bot_name, content=[Plain(header)])]
            for i, pth in enumerate(pages, 1):
                if not os.path.isfile(pth):
                    continue
                nodes.append(Node(
                    uin=bot_uin or "0",
                    name=bot_name,
                    content=[
                        Plain(f"📄 第 {i}/{len(pages)} 页 · {player_name} 全成就"),
                        ImgComp.fromFileSystem(pth),
                    ],
                ))
            try:
                yield event.chain_result([Nodes(nodes)])
            except Exception as e:
                logger.warning(f"[perfect] 合并转发失败，改逐页: {e}")
                yield event.plain_result(header)
                for pth in pages:
                    if os.path.isfile(pth):
                        yield event.image_result(pth)
            return

        # —— 模式 B：单款游戏成就列表 ——
        if not steamid:
            sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
            if len(sids) == 1:
                steamid = sids[0]
            elif sids:
                status_map = await self.fetch_player_statuses_batch(sids) if sids else {}
                for sid in sids:
                    st = status_map.get(sid) or {}
                    if str(st.get("gameid") or "") == str(appid):
                        steamid = sid
                        break
                steamid = steamid or sids[0]
        if not steamid:
            yield event.plain_result("未指定玩家。用法：/game ach <appid> @某人")
            return
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        yield event.plain_result("正在查询该游戏成就，请稍候...")
        player_name = self._resolve_player_display_name(steamid)
        game_name = await self.get_chinese_game_name(appid)
        unlocked = await self.achievement_monitor.get_player_achievements(
            self.API_KEY, group_id, steamid, appid
        )
        if unlocked is None:
            yield event.plain_result("获取成就失败：可能为隐私设置、无成就或 API 异常。")
            return
        details = await self.achievement_monitor.get_achievement_details(
            group_id, appid, lang="schinese", api_key=self.API_KEY, steamid=steamid
        )
        if not details:
            yield event.plain_result("未获取到该游戏的成就详情（可能无成就）。")
            return
        try:
            img_bytes = await render_achievement_list_image(
                details,
                unlocked,
                player_name=player_name,
                game_name=game_name or str(appid),
                font_path=self.get_font_path("NotoSansHans-Regular.otf"),
                proxy=self.proxy,
            )
        except Exception as e:
            import traceback
            logger.error(f"成就列表渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"渲染失败：{e}")
            return
        if not img_bytes:
            yield event.plain_result("渲染失败")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(img_bytes)
            yield event.image_result(tmp.name)
