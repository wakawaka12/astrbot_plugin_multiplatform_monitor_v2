from datetime import datetime
import tempfile
import time

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image

from ...shared.logging import logger
from ...shared.utils.notify_session import is_sendable_group_session
from ...presentation.renderers.game_start import render_game_start
from ...presentation.renderers.game_end import render_game_end


class NotificationTrackingMixin:
    """游戏状态通知的路由、渲染、缓冲和延迟结算编排。"""

    def _get_notify_sessions(self, group_id, sid):
        sessions = []
        normalized_group_id = str(group_id)
        normalized_sid = str(sid)
        notify_sessions = {
            str(key): value
            for key, value in (getattr(self, "notify_sessions", {}) or {}).items()
        }
        enabled = getattr(self, "group_monitor_enabled", {}) or {}

        def _group_enabled(gid):
            return enabled.get(str(gid), True)

        primary_session = notify_sessions.get(normalized_group_id)
        if primary_session and _group_enabled(normalized_group_id):
            sessions.append(primary_session)
        monitored_groups = {
            str(gid)
            for gid, steam_ids in getattr(self, "group_steam_ids", {}).items()
            if normalized_sid in {str(value) for value in steam_ids}
        }
        push_targets = (getattr(self, "push_groups", {}) or {}).get(normalized_sid)
        if push_targets is None:
            push_targets = next(
                (
                    targets
                    for raw_sid, targets in (getattr(self, "push_groups", {}) or {}).items()
                    if str(raw_sid) == normalized_sid
                ),
                [],
            )
        for push_gid in push_targets:
            normalized_push_gid = str(push_gid)
            push_session = notify_sessions.get(normalized_push_gid)
            if push_session and push_session not in sessions and _group_enabled(normalized_push_gid):
                sessions.append(push_session)
        return [
            session
            for session in sessions
            if is_sendable_group_session(session)
        ]

    async def _render_notification_image(self, noti):
        try:
            if noti["type"] == "start":
                status = noti.get("status", {})
                avatar_url = status.get("avatarfull") or status.get("avatar")
                superpower = self.get_today_superpower(noti["sid"])
                font_path = self.get_font_path("NotoSansHans-Regular.otf")
                # 多平台玩家（psn:/xbox:/nso:）：直接使用 presence 游戏名与封面，
                # 跳过 Steam 商店名称/封面/在线数查询
                from ...infrastructure.clients.multi import split_platform_sid
                is_multi = bool(split_platform_sid(str(noti["sid"])))
                if is_multi:
                    zh_game_name = noti.get("game") or "未知游戏"
                    en_game_name = zh_game_name
                    playtime_override = await self._psn_title_stats_minutes(
                        noti.get("sid"), zh_game_name
                    )
                    cover_url = status.get("cover_url")
                    # Xbox presence 常不带图：Titlehub → 按游戏名解析 Steam/SGDB 封面
                    if not cover_url and str(noti["sid"]).startswith("xbox:") and noti.get("gameid"):
                        try:
                            cover_url = await self.fetch_xbox_title_cover(str(noti["gameid"]))
                        except Exception as ce:
                            logger.warning(f"[Xbox] 开局封面 Titlehub 失败: {ce}")
                    if not cover_url:
                        try:
                            cover_url = await self.resolve_cover_by_game_name(zh_game_name)
                        except Exception as ce:
                            logger.warning(f"[Cover] 多平台名称封面失败: {ce}")
                    img_bytes = await render_game_start(
                        self.data_dir, noti["sid"], noti["name"], avatar_url,
                        noti["gameid"], zh_game_name, api_key=None,
                        superpower=superpower, sgdb_api_key=self.SGDB_API_KEY,
                        font_path=font_path, sgdb_game_name=en_game_name,
                        online_count=None, appid=noti.get("gameid"), proxy=self.proxy,
                        version=self._plugin_version, sgdb_api_base=self.SGDB_API_BASE,
                        cover_url=cover_url, skip_steam=True,
                        playtime_override=playtime_override,
                    )
                else:
                    zh_game_name, en_game_name = await self.get_game_names(noti["gameid"], noti["game"])
                    img_bytes = await render_game_start(
                        self.data_dir, noti["sid"], noti["name"], avatar_url,
                        noti["gameid"], zh_game_name, api_key=self.API_KEY,
                        superpower=superpower, sgdb_api_key=self.SGDB_API_KEY,
                        font_path=font_path, sgdb_game_name=en_game_name,
                        online_count=await self.get_game_online_count(noti["gameid"]),
                        appid=noti.get("gameid"), proxy=self.proxy,
                        version=self._plugin_version, sgdb_api_base=self.SGDB_API_BASE,
                    )
            else:
                end_time_str = datetime.fromtimestamp(noti["quit_time"]).strftime("%Y-%m-%d %H:%M")
                duration_h = noti["duration_min"] / 60 if noti["duration_min"] > 0 else 0
                zh_game_name, en_game_name = await self.get_game_names(noti["gameid"], noti["game"])
                from ...infrastructure.clients.multi import split_platform_sid as _sp
                is_multi_end = bool(_sp(str(noti["sid"])))
                end_cover_url = None
                if is_multi_end:
                    end_status = noti.get("status") or {}
                    end_cover_url = end_status.get("cover_url")
                    if not end_cover_url and str(noti["sid"]).startswith("xbox:") and noti.get("gameid"):
                        try:
                            end_cover_url = await self.fetch_xbox_title_cover(str(noti["gameid"]))
                        except Exception as ce:
                            logger.warning(f"[Xbox] 结束卡封面 Titlehub 失败: {ce}")
                    if not end_cover_url:
                        try:
                            end_cover_url = await self.resolve_cover_by_game_name(noti.get("game") or zh_game_name)
                        except Exception as ce:
                            logger.warning(f"[Cover] 结束卡名称封面失败: {ce}")
                img_bytes = await render_game_end(
                    self.data_dir, noti["sid"], noti["name"], noti.get("avatar_url"),
                    noti["gameid"], zh_game_name, end_time_str,
                    noti.get("tip_text") or "你已经和椅子合为一体，成为传说中的'椅子精'了喵！",
                    duration_h, sgdb_api_key=self.SGDB_API_KEY,
                    font_path=self.get_font_path("NotoSansHans-Regular.otf"),
                    sgdb_game_name=en_game_name, appid=noti.get("gameid"),
                    proxy=self.proxy, api_key=self.API_KEY,
                    sgdb_api_base=self.SGDB_API_BASE, steam_store_base=self.STEAM_STORE_BASE,
                    cover_url=end_cover_url, skip_steam=is_multi_end,
                )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                return tmp.name
        except Exception as e:
            logger.error(f"渲染通知图片失败 ({noti.get('type')}, {noti.get('name')}): {e}")
            return None

    async def _psn_title_stats_minutes(self, sid: str, game_name: str):
        """npsso 主人本人可查 title_stats；返回该游戏游玩分钟数（他人/失败返回 None）。
        PSN 不公开他人游玩时长，此能力仅对账号主人有效。"""
        from ...infrastructure.clients.multi import split_platform_sid
        sp = split_platform_sid(str(sid))
        if not sp or sp[0] != "psn":
            return None
        try:
            pool = self._ensure_psnawp()
        except Exception:
            return None
        if pool is None:
            return None

        def _get():
            try:
                me = pool.me()
                stats = me.title_stats()
                if not stats:
                    return None
                for t in stats:
                    nm = getattr(t, "name", None) or (t.get("name") if isinstance(t, dict) else None)
                    if nm and game_name and str(nm) in str(game_name):
                        minutes = getattr(t, "play_duration", None)
                        if isinstance(t, dict):
                            minutes = t.get("play_duration")
                        if minutes is None:
                            continue
                        # play_duration 单位通常为秒
                        return float(minutes) / 60 if float(minutes) > 3600 else float(minutes)
                return None
            except Exception:
                return None

        return await asyncio.to_thread(_get)

    def _notification_event_key(self, notification, session):
        event_time = notification.get("start_time")
        if notification.get("type") == "end":
            event_time = notification.get("quit_time")
        return (
            str(session),
            str(notification.get("sid")),
            str(notification.get("gameid")),
            str(notification.get("type")),
            int(event_time) if event_time is not None else None,
        )

    def _should_send_notification(self, notification, session):
        now = time.time()
        sent_events = getattr(self, "_sent_notification_events", None)
        if sent_events is None:
            sent_events = self._sent_notification_events = {}
        key = self._notification_event_key(notification, session)
        if key in sent_events and now - sent_events[key] < 600:
            return False
        sent_events[key] = now
        for old_key, sent_at in list(sent_events.items()):
            if now - sent_at >= 600:
                del sent_events[old_key]
        return True

    async def _send_merged_notification(self, group_id, notifications):
        if not notifications:
            return
        session_notifications = {}
        for notification in notifications:
            for session in self._get_notify_sessions(group_id, notification["sid"]):
                if not self._should_send_notification(notification, session):
                    logger.info(
                        "Skipping duplicate Steam status notification "
                        "(session=%s, sid=%s, gameid=%s, type=%s)",
                        session, notification.get("sid"),
                        notification.get("gameid"), notification.get("type"),
                    )
                    continue
                session_notifications.setdefault(session, []).append(notification)
        for session, matched_notifications in session_notifications.items():
            if not is_sendable_group_session(session):
                logger.warning(
                    "跳过无效通知会话 (group_id=%s, session=%r)",
                    group_id,
                    session,
                )
                continue
            msg_chain = []
            for notification in matched_notifications:
                if notification["type"] == "start":
                    line = f"🟢【{notification['name']}】开始游玩 {notification['game']}\n"
                else:
                    line = f"👋 {notification['name']} 不玩 {notification['game']}，游玩时间 {notification['duration_str']}\n"
                if self.config.get("notify_send_text", True):
                    msg_chain.append(Plain(line))
                if self.config.get("notify_send_image", True):
                    img_path = await self._render_notification_image(notification)
                    if img_path:
                        msg_chain.append(Image.fromFileSystem(img_path))
            if msg_chain:
                try:
                    await self.context.send_message(session, MessageChain(msg_chain))
                except Exception:
                    logger.exception(f"推送合并通知失败 (group_id={group_id}, session={session})")

    async def _flush_pending_end_notifications(self):
        if not self._pending_end_notifications:
            return
        pending = self._pending_end_notifications
        self._pending_end_notifications = {}
        for group_id, notifications in pending.items():
            if not (getattr(self, "group_monitor_enabled", {}) or {}).get(str(group_id), True):
                continue
            await self._send_merged_notification(group_id, notifications)
