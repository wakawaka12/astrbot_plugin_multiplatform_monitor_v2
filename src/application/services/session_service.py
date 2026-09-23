import asyncio
import re
from typing import Any, Dict, Optional, Tuple

from ...domain.monitoring.session import PlayingSession, apply
from ...presentation.formatters.status import format_play_duration


SessionKey = Tuple[str, str]


def _norm_game_name(name: str) -> str:
    s = str(name or "").strip().casefold()
    s = re.sub(r"[\s:：\-–—·™®©\[\]（）()!！?？。，,\.、'\"“”]", "", s)
    return s


class SessionService:
    """一局游戏的唯一应用层入口：apply、记账、发事件。"""

    # Steam 启动 Xbox/PS 同款游戏时，另一平台 presence 会同步亮起；
    # 同身份同游戏名在此窗口内只推一张开局卡
    CROSS_PLATFORM_DEDUP_SEC = 180

    def __init__(self, plugin):
        self._plugin = plugin
        # {(group_id, norm_game_name): {"ts": int, "sid": str, "player": str}}
        self._cross_start_marks: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def get(self, group_id, sid) -> Optional[PlayingSession]:
        return self._sessions().get(self._key(group_id, sid))

    def started_at(self, group_id, sid, gameid=None) -> Optional[int]:
        session = self.get(group_id, sid)
        if session is not None and session.state in ("playing", "confirming_exit"):
            if gameid is None or str(gameid) == str(session.gameid):
                return session.started_at
        return None

    async def handle(
        self,
        group_id,
        sid,
        observed_gameid,
        now,
        *,
        player_name="",
        current_game_name="未知游戏",
        status=None,
        skip_push=False,
    ):
        key = self._key(group_id, sid)
        current = self._sessions().get(key)
        snapshot = {"steamid": str(sid), "group_id": str(group_id), "gameid": observed_gameid}
        next_session, events = apply(
            current,
            snapshot,
            int(now),
            sid=str(sid),
            group_id=str(group_id),
        )
        self._store(group_id, sid, next_session)
        for event in events:
            await self._dispatch(
                event,
                player_name=player_name,
                current_game_name=current_game_name,
                status=status,
                skip_push=skip_push,
            )
        if (
            next_session is not None
            and next_session.state == "playing"
            and not skip_push
        ):
            await self._ensure_achievement_poll(
                next_session,
                player_name=player_name,
                game_name=current_game_name,
            )
        if events:
            self._plugin._data_dirty = True
        return next_session, events

    def tick_due(self, now: int):
        due = [
            (key, session)
            for key, session in list(self._sessions().items())
            if session.state == "confirming_exit"
            and session.exit_deadline is not None
            and int(now) >= session.exit_deadline
        ]
        for (group_id, sid), session in due:
            next_session, events = apply(
                session,
                {"steamid": sid, "group_id": group_id, "gameid": None},
                int(now),
                sid=sid,
                group_id=group_id,
            )
            self._store(group_id, sid, next_session)
            for event in events:
                self._dispatch_sync(event, skip_push=False)
            if events:
                self._plugin._data_dirty = True

    def discard_group(self, group_id):
        group_id = str(group_id)
        for key in [key for key in self._sessions() if key[0] == group_id]:
            self._sessions().pop(key, None)
            self._meta().pop(key, None)

    def discard_player(self, steam_id):
        steam_id = str(steam_id)
        for key in [key for key in self._sessions() if key[1] == steam_id]:
            self._sessions().pop(key, None)
            self._meta().pop(key, None)

    def hydrate_from_legacy(self, pending_all=None, start_all=None, last_all=None):
        pending_all = pending_all or {}
        start_all = start_all or {}
        last_all = last_all if last_all is not None else (getattr(self._plugin, "group_last_states", {}) or {})
        for group_id, pending_sids in pending_all.items():
            for sid, games in (pending_sids or {}).items():
                if self.get(group_id, sid) is not None:
                    continue
                info = self._pick_pending(games)
                if not info:
                    continue
                gameid, payload = info
                started_at = int(payload.get("start_time") or 0)
                if not started_at:
                    sid_times = (start_all.get(group_id) or {}).get(sid) or {}
                    if isinstance(sid_times, dict):
                        started_at = int(sid_times.get(gameid) or sid_times.get(str(gameid)) or 0)
                    elif sid_times:
                        started_at = int(sid_times)
                if not started_at:
                    continue
                quit_time = int(payload.get("quit_time") or started_at)
                session = PlayingSession(
                    sid=str(sid),
                    gameid=str(gameid),
                    started_at=started_at,
                    state="confirming_exit",
                    group_id=str(group_id),
                    exit_deadline=quit_time + 180,
                    exited_at=quit_time,
                )
                self._store(group_id, sid, session)
                self._meta()[self._key(group_id, sid)] = {
                    "player_name": payload.get("name") or str(sid),
                    "game_name": payload.get("game_name") or "未知游戏",
                    "avatar_url": None,
                }
        for group_id, sid_times in start_all.items():
            for sid, games in (sid_times or {}).items():
                if self.get(group_id, sid) is not None:
                    continue
                last = (last_all.get(group_id) or {}).get(sid) or {}
                current_gameid = str(last.get("gameid") or "") or None
                if not current_gameid or current_gameid == "0":
                    continue
                started_at = None
                if isinstance(games, dict):
                    started_at = games.get(current_gameid) or games.get(str(current_gameid))
                elif games:
                    started_at = games
                if not started_at:
                    continue
                session = PlayingSession(
                    sid=str(sid),
                    gameid=str(current_gameid),
                    started_at=int(started_at),
                    state="playing",
                    group_id=str(group_id),
                )
                self._store(group_id, sid, session)
                self._meta()[self._key(group_id, sid)] = {
                    "player_name": last.get("name") or str(sid),
                    "game_name": last.get("gameextrainfo") or "未知游戏",
                    "avatar_url": last.get("avatarfull") or last.get("avatar"),
                }

    def dump(self) -> Dict[str, Dict[str, Dict[str, Any]]]:
        payload = {}
        for (group_id, sid), session in self._sessions().items():
            if session.state == "closed":
                continue
            item = {
                "sid": session.sid,
                "gameid": session.gameid,
                "started_at": session.started_at,
                "state": session.state,
                "group_id": session.group_id,
                "exit_deadline": session.exit_deadline,
                "exited_at": session.exited_at,
                "closed_at": session.closed_at,
            }
            item.update(self._meta().get((group_id, sid), {}))
            payload.setdefault(group_id, {})[sid] = item
        return payload

    def load(self, payload: Optional[Dict[str, Any]]):
        self._sessions().clear()
        self._meta().clear()
        if not payload:
            return
        for group_id, sids in payload.items():
            for sid, item in (sids or {}).items():
                state = item.get("state") or "playing"
                if state == "closed":
                    continue
                session = PlayingSession(
                    sid=str(item.get("sid") or sid),
                    gameid=str(item.get("gameid") or ""),
                    started_at=int(item.get("started_at") or 0),
                    state=state,
                    group_id=str(item.get("group_id") or group_id),
                    exit_deadline=item.get("exit_deadline"),
                    exited_at=item.get("exited_at"),
                    closed_at=item.get("closed_at"),
                )
                if not session.gameid or not session.started_at:
                    continue
                self._store(group_id, sid, session)
                self._meta()[self._key(group_id, sid)] = {
                    "player_name": item.get("player_name") or str(sid),
                    "game_name": item.get("game_name") or "未知游戏",
                    "avatar_url": item.get("avatar_url"),
                }

    def _sessions(self) -> Dict[SessionKey, PlayingSession]:
        sessions = getattr(self._plugin, "playing_sessions", None)
        if sessions is None:
            sessions = {}
            self._plugin.playing_sessions = sessions
        return sessions

    def _meta(self) -> Dict[SessionKey, Dict[str, Any]]:
        meta = getattr(self._plugin, "_session_meta", None)
        if meta is None:
            meta = {}
            self._plugin._session_meta = meta
        return meta

    @staticmethod
    def _key(group_id, sid) -> SessionKey:
        return (str(group_id), str(sid))

    def _store(self, group_id, sid, session: Optional[PlayingSession]):
        key = self._key(group_id, sid)
        if session is None or session.state == "closed":
            self._sessions().pop(key, None)
        else:
            self._sessions()[key] = session

    @staticmethod
    def _pick_pending(games):
        if not isinstance(games, dict):
            return None
        for gameid, payload in games.items():
            if payload and not payload.get("notified"):
                return gameid, payload
        return None

    async def _dispatch(self, event, *, player_name, current_game_name, status, skip_push):
        if event.kind == "closed":
            self._on_closed(event.session, skip_push=skip_push)
            self._meta().pop(self._key(event.session.group_id, event.session.sid), None)
            return
        if event.kind == "started":
            key = self._key(event.session.group_id, event.session.sid)
            self._meta()[key] = {
                "player_name": player_name or event.session.sid,
                "game_name": current_game_name or "未知游戏",
                "avatar_url": (status or {}).get("avatarfull") or (status or {}).get("avatar"),
            }
            await self._on_started(
                event.session,
                player_name=player_name,
                game_name=current_game_name,
                status=status,
                skip_push=skip_push,
            )
            return
        if event.kind == "fluctuation":
            self._on_resumed(event.session, player_name=player_name, skip_push=skip_push)

    def _dispatch_sync(self, event, *, skip_push):
        if event.kind == "closed":
            self._on_closed(event.session, skip_push=skip_push)
            self._meta().pop(self._key(event.session.group_id, event.session.sid), None)

    def _on_closed(self, session: PlayingSession, *, skip_push: bool):
        plugin = self._plugin
        key = self._key(session.group_id, session.sid)
        meta = self._meta().get(key, {})
        game_name = meta.get("game_name") or "未知游戏"
        player_name = meta.get("player_name") or session.sid
        duration_min = session.duration_min
        plugin._record_playtime(session.sid, session.gameid, game_name, duration_min)
        plugin._record_session(
            sid=session.sid,
            gameid=session.gameid,
            game_name=game_name,
            start_time=session.started_at,
            end_time=session.exited_at or session.closed_at,
            duration_min=duration_min,
            group_id=session.group_id,
        )
        last_quit = plugin.group_last_quit_times.setdefault(session.group_id, {})
        last_quit.setdefault(session.sid, {})[session.gameid] = int(session.closed_at or session.exited_at or 0)

        task_key = (session.group_id, session.sid, session.gameid)
        poll_task = getattr(plugin, "achievement_poll_tasks", {}).pop(task_key, None)
        if poll_task:
            poll_task.cancel()
        # 结束瞬间捕获旧成就快照，传给延迟补偿作为对比基准（不被下面的 pop 清掉）
        snap = getattr(plugin, "achievement_snapshots", {}).get(task_key)
        getattr(plugin, "achievement_snapshots", {}).pop(task_key, None)
        achievement_monitor = getattr(plugin, "achievement_monitor", None)
        if achievement_monitor is not None:
            achievement_monitor.clear_game_achievements(session.group_id, session.sid, session.gameid)

        monitor_on = getattr(plugin, "group_monitor_enabled", {}).get(session.group_id, True)
        # 跨平台去重：开局被压制的会话，结束卡一并跳过
        end_suppressed = bool((meta or {}).get("cross_suppressed"))
        if end_suppressed:
            try:
                from ...shared.logging import logger as _lg
                _lg.info(f"[跨平台去重] 跳过结束推送 sid={session.sid} game={game_name}")
            except Exception:
                pass
        if not end_suppressed and not skip_push and monitor_on and not getattr(plugin, "_should_skip_game", lambda _gid: False)(session.gameid) and plugin.config.get("enable_game_end_notify", True):
            last_state = plugin.group_last_states.get(session.group_id, {}).get(session.sid) or {}
            plugin._pending_end_notifications.setdefault(session.group_id, []).append({
                "type": "end",
                "name": player_name,
                "game": game_name,
                "duration_str": format_play_duration(duration_min),
                "sid": session.sid,
                "gameid": session.gameid,
                "quit_time": session.exited_at or session.closed_at,
                "duration_min": duration_min,
                "avatar_url": meta.get("avatar_url") or last_state.get("avatarfull") or last_state.get("avatar"),
                "tip_text": plugin._end_game_tip(duration_min),
            })
        if not end_suppressed and not skip_push and monitor_on:
            delayed = getattr(plugin, "achievement_delayed_final_check", None)
            if delayed is not None:
                result = delayed(session.group_id, session.sid, session.gameid, player_name, game_name, achievements_a=snap)
                if asyncio.iscoroutine(result):
                    try:
                        asyncio.get_running_loop()
                    except RuntimeError:
                        pass
                    else:
                        asyncio.create_task(result)

    async def _on_started(self, session: PlayingSession, *, player_name, game_name, status, skip_push):
        plugin = self._plugin
        recent = plugin.group_recent_games.setdefault(session.group_id, [])
        if session.gameid not in recent:
            recent.append(session.gameid)
            if len(recent) > 8:
                recent.pop(0)
        monitor_on = getattr(plugin, "group_monitor_enabled", {}).get(session.group_id, True)
        # 跨平台同游戏去重（Steam 开 Xbox 游戏时 Xbox presence 也会亮）
        suppress = False
        if not skip_push:
            suppress = self._should_suppress_cross_platform_start(
                session.group_id, session.sid, game_name, session.started_at
            )
        if suppress:
            self._meta()[self._key(session.group_id, session.sid)]["cross_suppressed"] = True
            logger = getattr(plugin, "logger", None)
            # plugin 侧用 shared logger
            try:
                from ...shared.logging import logger as _lg
                _lg.info(
                    f"[跨平台去重] 跳过开局推送 sid={session.sid} game={game_name}（同游戏已在其他平台推送）"
                )
            except Exception:
                pass
        else:
            self._meta()[self._key(session.group_id, session.sid)]["cross_suppressed"] = False
        if not suppress and not skip_push and monitor_on and not getattr(plugin, "_should_skip_game", lambda _gid: False)(session.gameid) and plugin.config.get("enable_game_start_notify", True):
            plugin._pending_end_notifications.setdefault(session.group_id, []).append({
                "type": "start",
                "name": player_name or session.sid,
                "game": game_name or "未知游戏",
                "sid": session.sid,
                "gameid": session.gameid,
                "status": status or {},
                "start_time": session.started_at,
            })
        if skip_push:
            return
        if suppress:
            return
        await self._ensure_achievement_poll(session, player_name=player_name, game_name=game_name)

    def _qq_of_sid(self, sid: str) -> Optional[str]:
        plugin = self._plugin
        fn = getattr(plugin, "_qq_of_bound_sid", None)
        if callable(fn):
            return fn(sid)
        bind = getattr(plugin, "_bind_data", {}) or {}
        for qq, info in bind.items():
            if str((info or {}).get("sid") or "") == str(sid):
                return str(qq)
        return None

    def _should_suppress_cross_platform_start(
        self, group_id, sid, game_name, started_at
    ) -> bool:
        """同群、同身份（绑同 QQ 或同显示名）、同游戏名、窗口内已有其他平台开局 → 压制。"""
        norm = _norm_game_name(game_name)
        if not norm:
            return False
        now = int(started_at or 0)
        key = (str(group_id), norm)
        mark = self._cross_start_marks.get(key)
        if mark:
            same_sid = str(mark.get("sid")) == str(sid)
            within = abs(now - int(mark.get("ts") or 0)) <= self.CROSS_PLATFORM_DEDUP_SEC
            qq_a = self._qq_of_sid(sid)
            qq_b = self._qq_of_sid(mark.get("sid"))
            same_qq = bool(qq_a) and qq_a == qq_b
            same_player = (
                str(mark.get("player") or "") == str(
                    (self._meta().get(self._key(group_id, sid)) or {}).get("player_name") or ""
                )
            )
            if within and not same_sid and (same_qq or same_player):
                return True
        player = (self._meta().get(self._key(group_id, sid)) or {}).get("player_name") or str(sid)
        self._cross_start_marks[key] = {"ts": now, "sid": str(sid), "player": str(player)}
        # 简单清理过期
        if len(self._cross_start_marks) > 200:
            cutoff = now - 600
            self._cross_start_marks = {
                k: v for k, v in self._cross_start_marks.items()
                if int(v.get("ts") or 0) >= cutoff
            }
        return False

    async def _ensure_achievement_poll(self, session: PlayingSession, *, player_name, game_name):
        plugin = self._plugin
        if not plugin.config.get("enable_achievement_poll", True):
            return
        if getattr(plugin, "_should_skip_game", lambda _gid: False)(session.gameid):
            return
        key = (session.group_id, session.sid, session.gameid)
        if key in getattr(plugin, "achievement_poll_tasks", {}):
            return
        monitor = getattr(plugin, "achievement_monitor", None)
        if monitor is None:
            return
        from ...infrastructure.clients.multi import split_platform_sid
        sp = split_platform_sid(str(session.sid))
        if sp and sp[0] == "xbox":
            achievements = await plugin.fetch_xbox_title_achievements(sp[1], str(session.gameid))
        else:
            achievements = await monitor.get_player_achievements(
                plugin.API_KEY, session.group_id, session.sid, session.gameid
            )
        plugin.achievement_snapshots[key] = list(achievements or [])
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        plugin.achievement_poll_tasks[key] = asyncio.create_task(
            plugin.achievement_periodic_check(
                session.group_id, session.sid, session.gameid, player_name, game_name
            )
        )

    def _on_resumed(self, session: PlayingSession, *, player_name, skip_push):
        plugin = self._plugin
        if skip_push or not plugin.config.get("enable_network_fluctuation_notify", True):
            return
        notify_sessions = plugin._get_notify_sessions(session.group_id, session.sid)
        if not notify_sessions:
            return
        # 取当前游戏名（来自开始会话时缓存的 meta）
        _meta = self._meta().get(self._key(session.group_id, session.sid), {}) or {}
        game_name = _meta.get("game_name") or session.gameid

        async def _notify():
            from astrbot.api.event import MessageChain
            from astrbot.api.message_components import Plain

            name = player_name or session.sid
            for session_key in notify_sessions:
                try:
                    await plugin.context.send_message(
                        session_key,
                        MessageChain([Plain(f"📡 网络波动：{name} 在游玩 {game_name} 时重启游戏/网络波动了")]),
                    )
                except Exception:
                    pass

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.create_task(_notify())
