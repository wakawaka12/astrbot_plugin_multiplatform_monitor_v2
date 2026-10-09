import asyncio
import json
import os
import random
import tempfile
import time

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image, Plain

from ...shared.logging import format_exception, logger
from ...shared.utils.notify_session import is_sendable_group_session


class AchievementTrackingMixin:
    """成就轮询、结束补偿和成就通知的应用层编排。"""

    async def achievement_periodic_check(self, group_id, sid, gameid, player_name, game_name):
        key = (group_id, sid, gameid)
        try:
            _first = True
            while True:
                # 首轮立即对比；之后每 3 分钟一次（提速：原 20 分钟太慢）
                if not _first:
                    await asyncio.sleep(180)
                _first = False
                if gameid in self.achievement_blacklist:
                    logger.info(f"[成就定时对比] 游戏 {gameid} 已在黑名单，跳过轮询")
                    break
                achievements_a = self.achievement_snapshots.get(key)
                from ...infrastructure.clients.multi import split_platform_sid
                sp = split_platform_sid(str(sid))
                if sp and sp[0] == "xbox":
                    achievements_b = await self.fetch_xbox_title_achievements(sp[1], str(gameid))
                else:
                    achievements_b = await self.achievement_monitor.get_player_achievements(
                        self.API_KEY, group_id, sid, gameid
                    )
                today = time.strftime('%Y-%m-%d')
                fail_key = (gameid, today)
                if achievements_b is None:
                    cnt = self.achievement_fail_count.get(fail_key, 0) + 1
                    self.achievement_fail_count[fail_key] = cnt
                    logger.info(f"[成就] {player_name} 在 {game_name} 本轮获取失败（第{cnt}次），跳过本轮")
                    # 不再因累计失败拉黑（网络抖动会误伤有成就的游戏）；
                    # 是否拉黑由 achievement_monitor 依 API 结论决定
                    if cnt >= 10:
                        logger.info(f"[成就] 游戏 {gameid} 当天累计失败10次，本轮跳过（不拉黑）")
                        break
                    continue
                self.achievement_fail_count.pop(fail_key, None)
                if achievements_a is not None:
                    new_achievements = set(achievements_b) - set(achievements_a)
                    if new_achievements:
                        logger.info(f"[成就定时对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                        await self.notify_new_achievements(group_id, sid, player_name, gameid, game_name, new_achievements)
                        self.achievement_snapshots[key] = list(achievements_b)
                    else:
                        logger.info(f"[成就定时对比] {player_name} 在 {game_name} 未发现新成就")
                # 全成就检查（有上一轮快照才算"本次监控期内新达成"）
                try:
                    _total = await self._total_achievement_count(group_id, sid, gameid, game_name)
                    if _total > 0 and len(achievements_b) >= _total:
                        await self.notify_perfect_achievement(
                            group_id, sid, player_name, gameid, game_name, _total,
                            len(achievements_b), is_new=achievements_a is not None)
                except Exception as _pe:
                    logger.warning(f"[全成就] 检查异常: {_pe}")
        except asyncio.CancelledError:
            logger.info(f"[成就定时对比] 任务已取消 group_id={group_id} sid={sid} gameid={gameid}")
        except Exception as e:
            logger.error(f"[成就定时对比] group_id={group_id} sid={sid} gameid={gameid} 异常: {e}")

    async def achievement_delayed_final_check(self, group_id, sid, gameid, player_name, game_name, achievements_a=None):
        """结束补偿：多轮检查（30s / 60s / 120s，累计 30s / 90s / 210s）。

        提速背景：原来固定等 300 秒才检查，玩家从 Steam 弹通知到群里播报要 8 分钟。
        现改为 30 秒先查一轮（Steam 数据通常已同步 → 最快半分钟播报），
        并在 60s / 120s 各补一轮，容忍 Steam 同步较慢的情况。
        重复推送由两道保险挡住：新成就靠快照更新，全成就靠去重文件。
        """
        key = (group_id, sid, gameid)
        for _delay in (30, 60, 120):
            await asyncio.sleep(_delay)
            try:
                if await self._final_check_once(group_id, sid, gameid, player_name, game_name, achievements_a):
                    break
            except asyncio.CancelledError:
                raise
            except Exception as _e:
                logger.warning(f"[成就结束冗余对比] 轮次异常: {format_exception(_e)}")
        # 若该 key 已被新的成就轮询/会话占用（如重开同游戏或 A→B→A 切回），则跳过清理，避免误清新局数据
        if key in getattr(self, "achievement_poll_tasks", {}):
            return
        self.achievement_snapshots.pop(key, None)
        self.achievement_poll_tasks.pop(key, None)
        self.achievement_monitor.clear_game_achievements(group_id, sid, gameid)

    async def _final_check_once(self, group_id, sid, gameid, player_name, game_name, achievements_a) -> bool:
        """单轮结束对比。返回 True 表示可结束轮询（黑名单/已拿到数据）。"""
        key = (group_id, sid, gameid)
        if gameid in self.achievement_blacklist:
            logger.info(f"[成就结束冗余对比] 游戏 {gameid} 已在黑名单，跳过轮询")
            return True
        if achievements_a is None:
            achievements_a = self.achievement_snapshots.get(key)
        from ...infrastructure.clients.multi import split_platform_sid
        sp = split_platform_sid(str(sid))
        if sp and sp[0] == "xbox":
            achievements_b = await self.fetch_xbox_title_achievements(sp[1], str(gameid))
        else:
            achievements_b = await self.achievement_monitor.get_player_achievements(
                self.API_KEY, group_id, sid, gameid
            )
        fail_key = (gameid, time.strftime('%Y-%m-%d'))
        if achievements_b is None:
            cnt = self.achievement_fail_count.get(fail_key, 0) + 1
            self.achievement_fail_count[fail_key] = cnt
            logger.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 本轮获取失败（第{cnt}次），稍后补查")
            if cnt >= 10:
                logger.info(f"[成就] 游戏 {gameid} 当天累计失败10次，本轮跳过（不拉黑）")
                return True
            return False
        # 成功拿到数据 → 清零当天失败计数（计数语义改为「连续失败」）
        self.achievement_fail_count.pop(fail_key, None)
        # 1) 先发「新解锁的成就」（含最后那个）
        if achievements_a is not None:
            new_achievements = set(achievements_b) - set(achievements_a)
            if new_achievements:
                logger.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 解锁新成就：{', '.join(new_achievements)}")
                await self.notify_new_achievements(group_id, sid, player_name, gameid, game_name, new_achievements)
            else:
                logger.info(f"[成就结束冗余对比] {player_name} 在 {game_name} 未发现新成就")
        # 更新快照，避免下一轮重复推送同一批新成就
        self.achievement_snapshots[key] = list(achievements_b)
        # 2) 再发「全成就」（庆祝语 + 卡片，去重文件保证只发一次）
        if achievements_b:
            try:
                _total2 = await self._total_achievement_count(group_id, sid, gameid, game_name)
                if _total2 > 0 and len(achievements_b) >= _total2:
                    await self.notify_perfect_achievement(
                        group_id, sid, player_name, gameid, game_name, _total2,
                        len(achievements_b), is_new=achievements_a is not None)
            except Exception as _pe2:
                logger.warning(f"[全成就] 结束检查异常: {format_exception(_pe2)}")
        return True

    # ===== 全成就（Perfect / 100% 达成）推送 =====

    def _perfect_notified_path(self):
        return os.path.join(self.data_dir, "perfect_achievements_notified.json")

    def _load_perfect_notified(self):
        try:
            with open(self._perfect_notified_path(), encoding="utf-8") as f:
                return json.load(f) or {}
        except Exception:
            return {}

    def _save_perfect_notified(self, data):
        try:
            with open(self._perfect_notified_path(), "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception as e:
            logger.warning(f"[全成就] 保存去重记录失败: {e}")

    async def _total_achievement_count(self, group_id, sid, gameid, game_name="") -> int:
        """游戏总成就数（Steam 走 schema 缓存，Xbox 走其详情接口）。"""
        from ...infrastructure.clients.multi import split_platform_sid
        sp = split_platform_sid(str(sid))
        try:
            if sp and sp[0] == "xbox":
                d = await self.fetch_xbox_achievement_details(sp[1], str(gameid), game_name=game_name)
            else:
                d = self.achievement_monitor.details_cache.get((group_id, gameid))
                if not d:
                    d = await self.achievement_monitor.get_achievement_details(
                        group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=sid
                    )
            return len(d or {})
        except Exception as e:
            logger.debug(f"[全成就] 获取总成就数失败 appid={gameid}: {e}")
            return 0

    async def _get_hero_image(self, gameid) -> str:
        """游戏 hero 大图（1920x620）本地路径；失败回退普通封面。"""
        gid = str(gameid or "")
        if not gid.isdigit():
            return ""
        try:
            d = os.path.join(self.data_dir, "covers_hero")
            os.makedirs(d, exist_ok=True)
            p = os.path.join(d, f"{gid}.jpg")
            if os.path.exists(p) and os.path.getsize(p) > 20000:
                return p
            url = f"https://cdn.akamai.steamstatic.com/steam/apps/{gid}/library_hero.jpg"
            if await self._download_cover_to(url, p):
                return p
        except Exception as e:
            logger.debug(f"[全成就] hero 图获取失败 {gameid}: {e}")
        try:
            return await self.get_game_cover_url(gid) or ""
        except Exception:
            return ""

    async def notify_perfect_achievement(self, group_id, sid, player_name, gameid, game_name, total, unlocked_count, is_new=True, force=False):
        """达成全成就时单独推送一条通知（持久化去重，每账号每游戏仅一次）。"""
        try:
            if not self.group_achievement_enabled.get(group_id, True):
                return
            notified = self._load_perfect_notified()
            key = f"{sid}:{gameid}"
            if key in notified and not force:
                return
            # 去重完全依赖持久化文件（历史全成就已在其中登记为 silent）：
            # 走到这里说明该「账号+游戏」从未登记过 → 视为新达成并推送。
            # 不再依赖内存快照（重启/未建快照都会导致漏报，2026-10-09 实测踩过）
            if not is_new:
                logger.info(f"[全成就] {player_name}《{game_name}》无快照但未登记过，按新达成推送")
            sessions = []
            s0 = getattr(self, "notify_sessions", {}).get(group_id)
            if s0:
                sessions.append(s0)
            for push_gid in self.push_groups.get(sid, []):
                ps = getattr(self, "notify_sessions", {}).get(push_gid)
                if ps and ps not in sessions:
                    sessions.append(ps)
            sessions = [x for x in sessions if is_sendable_group_session(x)]
            if not sessions:
                return False
            _cheers = (
                "🎉 恭喜！完美主义玩家的又一枚勋章！",
                "🌟 全成就达成，实力与耐心兼备！",
                "👑 100% 完成度，这游戏算是被你玩明白了！",
                "🏅 成就猎人再添一座奖杯！",
                "🔥 一个不落，全部拿下！",
                "💎 完美通关，值得载入群史册！",
                "🎊 恭喜解锁「白金级玩家」称号！",
                "🚀 全成就打卡成功，下一款继续冲！",
                "🧠 毅力与技术的双重证明，牛！",
                "🍾 干杯！又是一款被彻底征服的游戏！",
                "✨ 全成就的光芒已笼罩本群！",
                "🥇 这一局的完美，属于你！",
            )
            text = (f"🏆【全成就达成】\n"
                    f"{player_name} 在《{game_name}》达成全成就！\n"
                    f"成就进度：{unlocked_count}/{total} (100%)")
            # 渲染祝贺卡片（大图，信息全在图内）
            card_tmp = None
            try:
                from ...presentation.renderers.perfect_card import render_perfect_card
                _hero = await self._get_hero_image(gameid)
                _reg = self.get_font_path("MiSans-Regular.ttf")
                _bold = self.get_font_path("MiSans-Bold.ttf")
                _png = render_perfect_card(player_name, game_name, unlocked_count, total,
                                           bg_path=_hero, regular_font=_reg, bold_font=_bold)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as _tf:
                    _tf.write(_png)
                    card_tmp = _tf.name
            except Exception as _ce:
                logger.error(f"[全成就] 卡片渲染失败: {_ce}")
            # 庆祝语 + 卡片合成一条消息（文本在上、图片在下）
            cheer_text = f"🎉 {random.choice(_cheers)}"
            chain = [Plain(cheer_text)]
            if card_tmp:
                chain.append(Image.fromFileSystem(card_tmp))
            ok_n = 0
            for session in sessions:
                try:
                    await self.context.send_message(session, MessageChain(chain))
                    ok_n += 1
                except Exception as e:
                    logger.error(f"[全成就] 发送失败 session={session}: {format_exception(e)}")
            if ok_n == 0:
                logger.warning("[全成就] 全部会话发送失败，不写入去重记录（下次轮询会重试）")
                return False
            if not force:
                notified[key] = {
                    "at": int(time.time()), "total": total,
                    "player": str(player_name or ""), "game": str(game_name or ""),
                }
                self._save_perfect_notified(notified)
            logger.info(f"[全成就] {player_name}《{game_name}》{unlocked_count}/{total} 已推送（成功 {ok_n}/{len(sessions)} 个会话{'，测试模式' if force else ''}）")
            return True
        except Exception as e:
            logger.error(f"[全成就] 通知异常: {e}")
            return False

    async def notify_new_achievements(self, group_id, steamid, player_name, gameid, game_name, new_achievements):
        if not self.group_achievement_enabled.get(group_id, True):
            return
        if not new_achievements or not self.notify_sessions:
            return
        achievements_to_notify = list(new_achievements)[:self.max_achievement_notifications]
        from ...infrastructure.clients.multi import split_platform_sid
        sp = split_platform_sid(str(steamid))
        details = None
        if sp and sp[0] == "xbox":
            details = await self.fetch_xbox_achievement_details(sp[1], str(gameid), game_name=game_name)
            # 渲染字段兼容：description -> desc
            if details:
                for d in details.values():
                    if "description" not in d and d.get("desc"):
                        d["description"] = d["desc"]
        else:
            details = self.achievement_monitor.details_cache.get((group_id, gameid))
            if not details:
                try:
                    details = await self.achievement_monitor.get_achievement_details(
                        group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=steamid
                    )
                except Exception as e:
                    details = None
                    logger.warning(f"获取成就详情失败: {e}")
        if details and game_name:
            for detail in details.values():
                detail["game_name"] = game_name
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        notify_sessions = []
        notify_session = getattr(self, 'notify_sessions', {}).get(group_id)
        if notify_session:
            notify_sessions.append(notify_session)
        for push_gid in self.push_groups.get(steamid, []):
            push_session = getattr(self, 'notify_sessions', {}).get(push_gid)
            if push_session and push_session not in notify_sessions:
                notify_sessions.append(push_session)
        notify_sessions = [
            session for session in notify_sessions
            if is_sendable_group_session(session)
        ]
        if not notify_sessions:
            logger.warning(
                "成就通知无有效会话，已跳过 (group_id=%s, steamid=%s)",
                group_id,
                steamid,
            )
            return
        tmp_path = None
        if self.config.get('notify_send_image', True) and details:
            if sp and sp[0] == "xbox":
                unlocked_set = await self.fetch_xbox_title_achievements(sp[1], str(gameid))
            else:
                unlocked_set = await self.achievement_monitor.get_player_achievements(
                    self.API_KEY, group_id, steamid, gameid
                )
            if not unlocked_set:
                unlocked_set = set(self.achievement_snapshots.get((group_id, steamid, gameid), []))
            try:
                img_bytes = await self.achievement_monitor.render_achievement_image(
                    details, set(achievements_to_notify), player_name=player_name,
                    steamid=steamid, appid=gameid, unlocked_set=unlocked_set or set(),
                    font_path=font_path,
                )
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
            except Exception as e:
                logger.error(f"成就图片渲染失败: {e}")
        if not tmp_path:
            return
        for session in notify_sessions:
            try:
                await self.context.send_message(session, MessageChain([Image.fromFileSystem(tmp_path)]))
            except Exception as e:
                logger.error(f"发送成就通知失败: {e}")
