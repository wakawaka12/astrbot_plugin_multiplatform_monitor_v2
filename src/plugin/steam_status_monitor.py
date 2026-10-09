from astrbot.api.star import Star, Context
from ..shared.logging import format_exception, logger, register_sensitive_values
from ..shared.network import (
    aclose_shared_httpx_clients,
    configure_tls,
    httpx_client_kwargs,
    requests_verify,
    set_status_only_mode,
    shared_httpx_client,
    status_httpx_client,
    status_only_mode_enabled,
    status_pool_stats,
)
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event import MessageChain
from astrbot.api.message_components import Plain, Image, Node, Nodes
import base64
import json
import time
import httpx
import asyncio
import os
import random
from ..application.services.openbox import handle_openbox
from ..application.services.steam_list import handle_steam_list
import re
from ..application.services.achievement_monitor import AchievementMonitor
from ..application.services.achievement_tracking import AchievementTrackingMixin
from ..application.services.notification_tracking import NotificationTrackingMixin
from ..application.services.session_quit import SessionQuitMixin
from ..application.services.status_change_tracking import StatusChangeTrackingMixin
from ..application.services.polling_tracking import PollingTrackingMixin
from ..presentation.renderers.game_start import render_game_start
from ..presentation.renderers.game_end import render_game_end
from ..presentation.renderers.rank import render_rank_image
from ..presentation.renderers.game_detail import render_game_detail_image
from ..presentation.renderers.game_start import get_font_path
from ..domain.monitoring import MonitorStateStore, StateBackedMonitorMixin
from ..domain.ranking.push_scopes import build_rank_push_scopes
from PIL import Image as PILImage
import io
from datetime import datetime, timedelta, date
import requests  # 新增导入
import tempfile
import traceback
import shutil
from ..presentation.web.admin_api import WebAdminAPI
from ..infrastructure.persistence.plugin_data import PersistenceMixin
from ..infrastructure.fonts import FontPackService
from ..infrastructure.clients.steam import (
    SteamClientMixin,
    note_steam_store_status,
    steam_store_ban_remaining,
    steam_store_blocked,
    steam_store_403_streak,
)
from ..infrastructure.clients.multi import MultiPlatformClientMixin, split_platform_sid
from ..infrastructure.clients.itad import ITADClient
from ..application.services.qq_menu_management import QQMenuManagementMixin
from ..application.services.game_wishlist_service import WishlistServiceMixin
from ..application.services.perfect_games_service import PerfectGamesServiceMixin
from ..application.services.game_price_service import GamePriceServiceMixin
from ..shared.utils.cache_age import format_cache_age
from ..shared.paths import ABILITIES_PATH, CONFIG_PATH
from ..shared.utils.price import extract_price_query, summary_to_cny, to_cny
from ..shared.utils.notify_session import is_sendable_group_session, is_valid_group_id

# 状态文件最后写入距今超过该秒数（默认 60 分钟），视为插件停止期间遗留的陈旧状态。
# 正常运行时 states.json 约每 5 分钟落盘一次（最慢轮询间隔 30 分钟 + 保存节流 5 分钟），
# 60 分钟阈值足以安全区分"插件停止过"与"正常运行"。
_STALE_STATE_THRESHOLD = 3600


class SteamStatusMonitorV3(
    QQMenuManagementMixin,
    PollingTrackingMixin,
    StatusChangeTrackingMixin,
    SessionQuitMixin,
    NotificationTrackingMixin,
    AchievementTrackingMixin,
    WishlistServiceMixin,
    PerfectGamesServiceMixin,
    GamePriceServiceMixin,
    StateBackedMonitorMixin,
    PersistenceMixin,
    SteamClientMixin,
    MultiPlatformClientMixin,
    Star,
):

    def __init__(self, context: Context, config=None):
        super().__init__(context)
        self.monitor_state = MonitorStateStore()
        # 插件运行状态标志，重启后自动丢失
        if hasattr(self, '_ssm_running') and self._ssm_running:
            logger.error("当前插件已在运行中。请重启astrbot而非重载插件")
            return
        self._ssm_running = True
        self._plugin_version = "4.5.5"
        self.context = context
        # 分群管理：所有状态数据均以 group_id 为 key
        self.group_steam_ids = {}         # {group_id: [steamid, ...]}
        self.group_last_states = {}       # {group_id: {steamid: status}}
        self.group_last_quit_times = {}   # {group_id: {steamid: {gameid: quit_time}}}
        self.group_pending_logs = {}      # {group_id: {steamid: {gameid: log_dict}}}
        self.group_recent_games = {}      # {group_id: [gameid, ...]}
        self._session_meta = {}           # {(group_id, sid): {player_name, game_name, avatar_url}}
        # 超能力缓存和能力列表
        self._superpower_cache = {}  # {(steamid, date): superpower}
        self._abilities = None
        self._abilities_path = str(ABILITIES_PATH)
        self._game_name_cache = {}  # 修复: 游戏名缓存，防止 AttributeError
        # 统一使用 AstrBot 配置系统
        self.config = config or {}
        # 兼容旧逻辑，若 config 为空则尝试读取 config.json（可选，建议后续移除）
        if not self.config:
            try:
                config_path = str(CONFIG_PATH)
                with open(config_path, 'r', encoding='utf-8') as f:
                    self.config = json.load(f)
            except Exception as e:
                logger.error(f"steam_status_monitor 配置读取失败: {e}")
                self.config = {}
        # 旧配置迁移：如存在 steam_ids（未分群），迁移到 group_steam_ids['default']
        if 'steam_ids' in self.config and 'group_steam_ids' not in self.config:
            steam_ids = self.config.get('steam_ids', [])
            if isinstance(steam_ids, str):
                steam_ids = [x.strip() for x in steam_ids.split(',') if x.strip()]
            self.config['group_steam_ids'] = {'default': steam_ids}
            self.config.pop('steam_ids', None)
            logger.info(f"已自动迁移旧 steam_ids 配置到 group_steam_ids['default']")
        # 读取配置项，提供默认值
        self.API_KEY = self.config.get('steam_api_key', '')
        register_sensitive_values(self.API_KEY, self.config.get('sgdb_api_key', ''))
        # 多平台配置（PSN / Xbox / NSO）
        self.psn_npsso = self.config.get('psn_npsso', '') or ''
        self.xbox_config = {
            "client_id": self.config.get('xbox_client_id', '') or '',
            "client_secret": self.config.get('xbox_client_secret', '') or '',
            "tokens_file": self.config.get('xbox_tokens_file', '') or '',
        }
        self.nso_http_base = self.config.get('nso_http_base', '') or ''
        register_sensitive_values(self.psn_npsso, self.xbox_config.get('client_secret', ''))
        self.SSL_CA_FILE = self.config.get('ssl_ca_file', '')
        try:
            configure_tls(self.SSL_CA_FILE)
        except ValueError as exc:
            logger.error(f"TLS 配置无效，将使用系统默认信任链: {exc}")
            self.SSL_CA_FILE = ''
            configure_tls()
        # API Base URL（支持自定义，默认官方地址）
        self.STEAM_API_BASE = (self.config.get('steam_api_base', '') or 'https://api.steampowered.com').rstrip('/')
        self.STEAM_STORE_BASE = (self.config.get('steam_store_base', '') or 'https://store.steampowered.com').rstrip('/')
        self.SGDB_API_BASE = (self.config.get('sgdb_api_base', '') or 'https://www.steamgriddb.com').rstrip('/')
        self.group_steam_ids = self.config.get('group_steam_ids', {})
        self.RETRY_TIMES = self.config.get('retry_times', 3)
        # 代理支持（来自 PR #16 by Sodiumsss）
        self.ENABLE_PROXY = self.config.get('enable_proxy', False)
        self.PROXY_URL = self.config.get('proxy_url', '')
        self.proxy = self.PROXY_URL if self.ENABLE_PROXY and self.PROXY_URL else None
        self.ITAD_CLIENT = ITADClient(
            self.config.get('itad_api_key', ''),
            proxy=self.proxy,
            base_url=self.config.get('itad_api_base', ''),
        )
        self._steam_search_cache = {}
        self._steam_search_pending = {}
        self._translate_lock = asyncio.Lock()
        self._translate_cache = {}
        # 代理前置校验：若启用 SOCKS 代理但未安装 socksio，尝试自动安装
        if self.proxy and self.proxy.startswith('socks'):
            try:
                import socksio
            except ImportError:
                logger.info(f'[SteamStatusMonitor] 检测到 SOCKS 代理 ({self.proxy})，socksio 未安装，尝试自动安装...')
                import subprocess, sys
                try:
                    subprocess.check_call(
                        [sys.executable, '-m', 'pip', 'install', 'httpx[socks]', '-q'],
                        timeout=60
                    )
                    import socksio
                    logger.info('[SteamStatusMonitor] socksio 自动安装成功')
                except Exception as ie:
                    logger.error(
                        f'[SteamStatusMonitor] socksio 自动安装失败: {ie}。'
                        f'请手动执行: pip install httpx[socks]'
                    )
        self.max_group_size = self.config.get('max_group_size', 20)
        self.GROUP_ID = None  # 当前操作群号，指令时动态赋值
        self.fixed_poll_interval = self.config.get('fixed_poll_interval', 0)  # 新增：固定轮询间隔，0为智能轮询
        self.poll_interval_mid_sec = self.config.get('poll_interval_mid_sec', 600)  # 10分钟
        self.poll_interval_long_sec = self.config.get('poll_interval_long_sec', 1800)  # 30分钟
        self.next_poll_time = {}  # {group_id: {steamid: next_time}}
        self.detailed_poll_log = self.config.get('detailed_poll_log', True)
        # 新增：智能轮询间隔配置 [游戏中, 12分钟内, 12分钟~3小时, 3小时~24小时, 24~48小时, 超过48小时]
        raw_intervals = self.config.get('smart_poll_intervals', "1,3,5,10,20,30")
        if isinstance(raw_intervals, str):
            self.smart_poll_intervals = [int(x.strip()) for x in raw_intervals.split(",") if x.strip()]
        else:
            self.smart_poll_intervals = list(raw_intervals)
        # 归一化回字符串写入 config，防止 WebUI schema 校验类型错误
        self.config['smart_poll_intervals'] = ",".join(str(x) for x in self.smart_poll_intervals)
        # 数据持久化目录
        self.data_dir = os.path.join("data", "steam_status_monitor")
        os.makedirs(self.data_dir, exist_ok=True)
        self.font_pack = FontPackService(
            self.data_dir,
            proxy=self.proxy,
            enabled=bool(self.config.get("font_download_enabled", True)),
            pack_url=str(self.config.get("font_pack_url", "") or ""),
            timeout_sec=int(self.config.get("font_download_timeout_sec", 600) or 600),
        )
        self._font_pack_task = self.font_pack.ensure_ready()
        self._load_group_steam_ids()  # 新增：优先从 steam_groups.json 加载
        self._load_persistent_data()
        self._load_notify_session()
        # 成就监控
        self.achievement_monitor = AchievementMonitor(self.data_dir, steam_api_base=self.STEAM_API_BASE, proxy=self.proxy)
        self.max_achievement_notifications = self.config.get('max_achievement_notifications', 5)
        self.achievement_poll_tasks = {}  # {(group_id, sid, gameid): asyncio.Task}
        self.achievement_snapshots = {}   # {(group_id, sid, gameid): [成就列表]}
        self.achievement_blacklist = set()  # 新增：成就查询黑名单
        self.achievement_fail_count = {}    # 新增：成就查询失败计数
        # --- 新增：重启后自动推送 ---
        self.running_groups = set()  # 正在运行的群号集合
        self.group_monitor_enabled = {}      # {group_id: bool} 监控开关
        self.group_achievement_enabled = {}  # {group_id: bool} 成就推送开关
        self._load_group_switches()
        self._qq_menu_lock = asyncio.Lock()
        self._platform_id = None  # 记录消息平台ID，用于WebUI自动补全通知目标
        # --- WebUI 群自动补全 notify_sessions ---
        self._auto_fill_notify_sessions()
        # --- 新增：重启后自动恢复所有群的轮询 ---
        if hasattr(self, 'notify_sessions') and self.notify_sessions and self.API_KEY and self.group_steam_ids:
            logger.info(f"[SteamStatusMonitor] 检测到 notify_sessions={self.notify_sessions}，自动启动监控轮询")
            for group_id in self.notify_sessions:
                if group_id in self.group_steam_ids and self.group_monitor_enabled.get(group_id, True):
                    self.running_groups.add(group_id)
        # --- 新增：全局日志收集与统一输出 ---
        self._last_round_logs = []  # [(group_id, logstr)]
        # --- 新增：持久化数据脏标志 + 节流保存，避免高频写盘拖慢主循环 ---
        self._data_dirty = False          # 有变更待保存
        self._last_save_time = time.time() # 上次保存时间戳
        self._save_interval = 300          # 节流间隔（秒），300秒=5分钟
        # --- 插件启动时间戳 + 启动初始化期间的"陈旧群"标记（init 完成后清空） ---
        self._startup_time = time.time()
        self._startup_stale_groups = {}
        # 保存任务引用，便于 terminate 时取消，防止重载/禁用后残留多实例并发
        self._poll_loop_task = asyncio.create_task(self.global_poll_and_log_loop())
        self._init_poll_task = asyncio.create_task(self.init_poll_time_once())
        # 头像静默刷新：每天凌晨后台重下，成功才替换缓存
        self.avatar_refresh_hour = int(self.config.get('avatar_refresh_hour', 4) or 4)
        self._avatar_refresh_task = asyncio.create_task(self._avatar_refresh_loop())
        # SGDB API Key 可在 https://www.steamgriddb.com/profile/preferences/api 获取
        self.SGDB_API_KEY = self.config.get('sgdb_api_key', '')
        self._load_push_groups()  # <--- 修复：确保push_groups属性初始化
        # --- 排行榜功能：游玩时长记录 + 去重缓存 + 每日推送开关 ---
        self.play_records = {}              # {date_str: {steamid: {gameid: {name, minutes}}}}
        self.session_records = {}           # {steamid: [session_dict]} 甘特图/热力图数据
        self._session_dirty = False         # session 数据脏标志
        self._recorded_quit_cache = {}      # {(steamid, gameid): timestamp} 去重用
        self.rank_push_groups = []          # 开启了每日排行榜推送的群列表
        self.rank_push_all = False           # True=全群统一推送全局排行（只渲染一次）
        self.rank_push_hour = self.config.get('rank_push_hour', 8)
        self.rank_push_minute = self.config.get('rank_push_minute', 30)
        self._last_rank_push_date = None    # 记录上次推送日期，防止同一天重复推送
        self._load_play_records()
        self._load_session_records()
        self._load_rank_push_groups()
        # QQ-SteamID 绑定数据
        self._bind_data = {}  # {qq: {sid, nickname}}
        self._load_bind_data()
        # --- Steam 已购库快照（每小时对比检测新购游戏） ---
        self.owned_games_snapshot = {}  # {steamid: {appid: name, ...}}
        self.owned_games_log = []       # [{sid, name, game_name, date}]
        self._load_owned_games_data()
        self._owned_games_task = asyncio.create_task(self._owned_games_loop())
        # --- 愿望单打折推送 ---
        self.wish_sale_enabled_groups = set()  # {group_id} 开启了愿望单打折推送的群
        self.wish_sale_last_cuts = {}           # {sid: {appid: cut}}
        self.wish_sale_log = []                 # [{sid, player_name, appid, name, cut, price, date}]
        self.wish_sale_scan_pos = {}            # {sid: int} 多轮扫描游标
        self._wish_sale_cache = {}              # {sid: {"ts": float, "items": [...]}}
        self._wish_store_ban_until = 0.0        # 愿望单 SSR 冷却截止时间戳
        self._wish_store_403_streak = 0
        self._load_wish_sale_data()
        self._wish_sale_task = asyncio.create_task(self._wish_sale_loop())
        # --- Steam API 扫描护栏：全库扫描期间暂停其它重查询，防风控 ---
        self._steam_api_guard = None  # {"label": str, "ts": float}
        self._steam_scan_state = None  # 扫描任务：label/umo/cancel/started
        # --- 通知合并缓冲区：SessionService 将开始/结束通知写入此队列，由主轮询统一 flush ---
        self._pending_end_notifications = {}  # {group_id: [notification_dict, ...]}
        # --- AstrBot Plugin Pages 管理后台 ---
        self.web_api = WebAdminAPI(self)
        self.web_api.register_routes(context)
        logger.info("[WebAdmin] 管理页面已注册到 AstrBot 内置 WebUI")

    def _begin_steam_guard(self, label: str, umo: str = ""):
        """标记 Steam API 重查询进行中（如全库成就扫描）。"""
        self._steam_api_guard = {"label": str(label or "Steam资料扫描"), "ts": time.time()}
        self._steam_scan_state = {
            "label": str(label or "Steam资料扫描"),
            "umo": str(umo or ""),
            "cancel": asyncio.Event(),
            "started": time.time(),
        }

    def _end_steam_guard(self):
        self._steam_api_guard = None
        self._steam_scan_state = None

    def _steam_scan_cancel_event(self):
        st = getattr(self, "_steam_scan_state", None)
        if not st:
            return None
        return st.get("cancel")

    def steam_guard_active(self) -> bool:
        g = getattr(self, "_steam_api_guard", None)
        return bool(g)

    def _status_only_mode(self) -> bool:
        """扫描期间仅保留状态推送（上下线）；愿望单/背景扩展任务跳过。"""
        try:
            return status_only_mode_enabled(getattr(self, "data_dir", "") or "")
        except Exception:
            return False

    def _steam_guard_block_msg(self) -> str:
        """扫描期间其它 Steam 查询指令的统一提示；空串表示未锁定。"""
        g = getattr(self, "_steam_api_guard", None)
        if not g:
            return ""
        label = g.get("label") or "Steam资料扫描"
        elapsed = int(time.time() - float(g.get("ts") or 0))
        return (
            f"⏳ 正在{label}（已进行约 {elapsed} 秒），为降低 Steam 风控，"
            f"其它查库/查价/愿望单/排行等接口暂不可用。\n"
            f"绑定查询、帮助等本地功能仍可用。可用 /game ach stop 手动停止扫描。"
        )

    def _steam_guard_or_none(self) -> str:
        return self._steam_guard_block_msg()

    def _is_group_state_stale(self, group_id, threshold=_STALE_STATE_THRESHOLD):
        """判断该群状态缓存是否为插件停止期间遗留的旧数据。

        依据：states.json 最后写入时间早于本次插件启动，且距今超过阈值（默认 60 分钟）。
        正常运行时 states.json 约每 5 分钟落盘一次（mtime 持续刷新）；插件停止后 mtime
        停在停止时刻。重启后首次初始化期间用它识别"停止期间累积的历史变化"，跳过播报。
        """
        try:
            path = self._get_group_data_path(group_id, "states")
            if not os.path.exists(path):
                return False  # 无缓存文件，无从判断，视为正常
            mtime = os.path.getmtime(path)
            return mtime < self._startup_time and (time.time() - mtime) > threshold
        except Exception as e:
            logger.warning(f"[陈旧状态] 判断 states 新鲜度失败: {e} (group_id={group_id})")
            return False

    async def terminate(self):
        '''插件被卸载/停用时取消所有后台任务并保存持久化数据'''
        # 取消主轮询循环和初始化任务，防止重载/禁用后残留多实例并发
        for t in (
            getattr(self, '_poll_loop_task', None),
            getattr(self, '_init_poll_task', None),
            getattr(self, '_font_pack_task', None),
            getattr(self, '_avatar_refresh_task', None),
            getattr(self, '_owned_games_task', None),
            getattr(self, '_wish_sale_task', None),
        ):
            if t and not t.done():
                t.cancel()
        font_pack = getattr(self, 'font_pack', None)
        if font_pack:
            await font_pack.aclose()
        try:
            await aclose_shared_httpx_clients()
        except Exception as e:
            logger.warning(f"[network] 关闭共享 HTTP 连接池失败: {e}")
        if hasattr(self, 'achievement_poll_tasks'):
            for task in self.achievement_poll_tasks.values():
                task.cancel()
            self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        # 保存持久化数据（强制落盘，不节流）
        self._save_persistent_data(force=True)
        # 重置运行标志，允许下次重载正常初始化
        self._ssm_running = False

    async def _avatar_refresh_loop(self):
        """每天凌晨静默刷新头像缓存；失败不影响渲染（继续用旧图）。"""
        import datetime as _dt
        while True:
            try:
                now = _dt.datetime.now()
                target = now.replace(hour=self.avatar_refresh_hour, minute=0, second=0, microsecond=0)
                if target <= now:
                    target += _dt.timedelta(days=1)
                delay = (target - now).total_seconds()
                await asyncio.sleep(delay)
                await self._refresh_all_avatars()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[头像刷新] 后台任务异常，1小时后重试")
                await asyncio.sleep(3600)

    async def _refresh_all_avatars(self):
        """收集当前监控玩家头像 URL，后台重下并原子替换缓存。"""
        sids = []
        seen = set()
        for ids in self.group_steam_ids.values():
            for sid in ids:
                sid = str(sid)
                if sid not in seen:
                    seen.add(sid)
                    sids.append(sid)
        if not sids:
            return
        status_map = {}
        try:
            status_map = await self.fetch_player_statuses_batch(sids) or {}
        except Exception as e:
            logger.warning(f"[头像刷新] 状态批量查询失败，本轮跳过: {e}")
            return
        todo = []
        for sid in sids:
            st = status_map.get(sid) or {}
            url = st.get('avatarfull') or st.get('avatar') or ''
            if url:
                todo.append((sid, url))
        if not todo:
            logger.info("[头像刷新] 无可用头像 URL，跳过")
            return
        ok = fail = skip = 0
        sem = asyncio.Semaphore(6)

        async def _one(sid, url):
            nonlocal ok, fail, skip
            async with sem:
                path = os.path.join(self.data_dir, "avatars", f"{sid}.jpg")
                try:
                    async with shared_httpx_client(proxy=self.proxy, timeout=12, follow_redirects=False) as client:
                        resp = await client.get(url)
                    if resp.status_code != 200 or not resp.content:
                        fail += 1
                        return
                    img = PILImage.open(io.BytesIO(resp.content))
                    img.verify()  # 校验完整
                    # 校验通过后原子替换
                    tmp = path + ".tmp"
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(tmp, "wb") as f:
                        f.write(resp.content)
                    os.replace(tmp, path)
                    ok += 1
                except Exception:
                    fail += 1
                    try:
                        if os.path.exists(path + ".tmp"):
                            os.remove(path + ".tmp")
                    except Exception:
                        pass

        await asyncio.gather(*(_one(sid, url) for sid, url in todo))
        logger.info(f"[头像刷新] 完成：成功 {ok} · 失败 {fail} · 总数 {len(todo)}（失败继续用旧缓存）")

    # ========== Steam 已购库快照对比 ==========

    def _load_owned_games_data(self):
        path = os.path.join(self.data_dir, "owned_games_data.json")
        try:
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
                self.owned_games_snapshot = data.get("snapshot") or {}
                self.owned_games_log = data.get("log") or []
        except Exception as e:
            logger.warning(f"[购游戏] 加载数据失败: {e}")
            self.owned_games_snapshot = {}
            self.owned_games_log = []

    def _save_owned_games_data(self):
        path = os.path.join(self.data_dir, "owned_games_data.json")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"snapshot": self.owned_games_snapshot, "log": self.owned_games_log}, f, ensure_ascii=False)
        except Exception as e:
            logger.warning(f"[购游戏] 保存数据失败: {e}")

    async def _owned_games_loop(self):
        """每小时对比 Steam 已购库，检测新购游戏并推送。"""
        await asyncio.sleep(90)
        while True:
            try:
                if self.steam_guard_active():
                    logger.info("[购游戏] Steam API 扫描中，本轮跳过已购库对比")
                elif not self._owned_games_notify_enabled():
                    logger.debug("[购游戏] enable_owned_games_notify=false，跳过")
                else:
                    logger.info("[购游戏] 开始对比已购库（检测新购）")
                    await self._check_new_owned_games()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("[购游戏] 轮询异常")
            await asyncio.sleep(3600)

    def _owned_games_notify_enabled(self) -> bool:
        try:
            return bool((self.config or {}).get("enable_owned_games_notify", True))
        except Exception:
            return True

    async def _check_new_owned_games(self):
        """对比所有监控 Steam 玩家的已购库，检测新增游戏并推送 + 写入本地库。"""
        from ..infrastructure.persistence import local_store as lstore
        sids = []
        for ids in self.group_steam_ids.values():
            for sid in ids:
                sid = str(sid)
                if sid.isdigit() and len(sid) == 17:
                    sids.append(sid)
        sids = list(dict.fromkeys(sids))
        if not sids:
            logger.info("[购游戏] 无监控 SteamID")
            return
        today = date.today().isoformat()
        new_games_all = []
        conn = None
        try:
            conn = lstore.get_store(getattr(self, "data_dir", "") or "")
        except Exception:
            conn = None
        for sid in sids:
            games = await self.fetch_owned_games(sid)
            if games is None:
                logger.warning(f"[购游戏] sid={sid} 库存不可读（隐私/接口失败）")
                continue
            current = {str(g["appid"]): g["name"] for g in games if g.get("appid")}
            # 本地库：库存快照 + 游戏名
            if conn is not None:
                try:
                    lstore.set_owned_games(conn, sid, list(current.keys()), names=current)
                    for aid, nm in list(current.items())[:500]:
                        lstore.upsert_game(conn, appid=aid, name=str(nm or ""), content_type="")
                    conn.commit()
                except Exception as e:
                    logger.debug(f"[购游戏] 写本地库失败 sid={sid}: {e}")
            old = self.owned_games_snapshot.get(sid) or {}
            if not old:
                self.owned_games_snapshot[sid] = current
                logger.info(f"[购游戏] sid={sid} 首次快照 {len(current)} 款（只记录不推送）")
                continue
            new_appids = [aid for aid in current if aid not in old]
            if not new_appids:
                self.owned_games_snapshot[sid] = current
                continue
            for aid in new_appids:
                game_name = current[aid]
                new_games_all.append({"sid": sid, "game_name": game_name, "appid": aid, "date": today})
                self.owned_games_log.append({"sid": sid, "game_name": game_name, "appid": aid, "date": today})
                if conn is not None:
                    try:
                        lstore.upsert_game(conn, appid=aid, name=str(game_name or ""), content_type="")
                    except Exception:
                        pass
            self.owned_games_snapshot[sid] = current
            logger.info(f"[购游戏] sid={sid} 新购 +{len(new_appids)}: {[current[a] for a in new_appids[:8]]}")
            await asyncio.sleep(2)
        if conn is not None:
            try:
                conn.commit()
            except Exception:
                pass
        if new_games_all:
            self.owned_games_log = self.owned_games_log[-200:]
            self._save_owned_games_data()
            await self._push_new_owned_games(new_games_all)
        else:
            self._save_owned_games_data()
            logger.info("[购游戏] 本轮无新购")

    async def _push_new_owned_games(self, new_games):
        """推送新购游戏通知：同一批（同一推送目标）只发一条图文。"""
        if not new_games:
            return
        from ..presentation.renderers.owned_push import (
            attach_icons,
            build_text_summary,
            render_owned_push_card,
        )

        # 同一 umo 可能收多个玩家的新增：按推送目标合并
        by_umo = {}
        zh_name_cache = {}
        for item in new_games:
            sid = str(item.get("sid") or "")
            player_name = self._resolve_player_display_name(sid)
            appid = item.get("appid")
            raw_name = item.get("game_name") or "?"
            # 有中文用中文（带缓存，避免同款游戏重复打商店 API）
            display_name = raw_name
            cache_key = str(appid) if appid is not None else raw_name
            if cache_key in zh_name_cache:
                display_name = zh_name_cache[cache_key]
            else:
                try:
                    zh = await self.get_chinese_game_name(appid, raw_name)
                    if zh and str(zh).strip():
                        display_name = str(zh).strip()
                    zh_name_cache[cache_key] = display_name
                except Exception:
                    zh_name_cache[cache_key] = display_name
            entry = {
                "sid": sid,
                "player_name": player_name,
                "game_name": display_name,
                "raw_name": raw_name,
                "appid": appid,
                "date": item.get("date"),
            }
            targets = await self._push_targets_for_player(sid)
            if not targets:
                # 无推送目标时兜底主监控群 notify
                for gid, ids in (self.group_steam_ids or {}).items():
                    if sid in {str(x) for x in ids}:
                        for s in self._get_notify_sessions(gid, sid):
                            targets = list(targets or []) + [s]
            for umo in targets or []:
                bucket = by_umo.setdefault(umo, [])
                key = (entry["sid"], str(entry["appid"]))
                if key not in {(x["sid"], str(x["appid"])) for x in bucket}:
                    bucket.append(entry)

        for umo, items in by_umo.items():
            if not items:
                continue
            # 去重后补头像/封面/图标并渲染
            avatar_urls = {}
            for it in items:
                sid = str(it.get("sid") or "")
                for states in (getattr(self, "group_last_states", {}) or {}).values():
                    st = (states or {}).get(sid) or {}
                    url = st.get("avatarfull") or st.get("avatar")
                    if url:
                        avatar_urls[sid] = url
                        break
            try:
                items = await attach_icons(
                    items,
                    data_dir=self.data_dir,
                    proxy=self.proxy,
                    avatar_urls=avatar_urls,
                )
            except Exception as e:
                logger.warning(f"[购游戏] 图标预取失败: {e}")
            img_bytes = None
            try:
                img_bytes = render_owned_push_card(
                    items,
                    font_path=self.get_font_path("NotoSansHans-Regular.otf"),
                )
            except Exception as e:
                logger.error(f"[购游戏] 批量卡片渲染失败: {e}")

            text = build_text_summary(items)
            try:
                if img_bytes:
                    import tempfile
                    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                        tmp.write(img_bytes)
                        tmp_path = tmp.name
                    chain = MessageChain([
                        Plain(text),
                        Image.fromFileSystem(tmp_path),
                    ])
                else:
                    # 渲染失败时退回纯文本，仍只发一条
                    detail = "；".join(f"{it['player_name']}《{it['game_name']}》" for it in items[:10])
                    if len(items) > 10:
                        detail += f" 等{len(items)}款"
                    chain = MessageChain().message(f"{text}\n{detail}")
                await self.context.send_message(umo, chain)
                names = "、".join(f"{it['player_name']}:{it['game_name']}" for it in items[:5])
                logger.info(f"[购游戏] 批量推送 {len(items)} 条 -> {umo} | {names}")
            except Exception as e:
                logger.error(f"[购游戏] 推送失败: {format_exception(e)}")

    def _cached_persona_name(self, sid) -> str:
        sid = str(sid)
        mem = getattr(self, "_steam_persona_names", None) or {}
        if mem.get(sid):
            return str(mem[sid])
        return self._cached_persona_name_from_disk(sid)

    @staticmethod
    def _is_steamid_like(name) -> bool:
        """仅当像完整 SteamID64 时才算「数字 ID」；短数字昵称如 74 是合法名字。"""
        s = str(name or "").strip()
        return len(s) == 17 and s.isdigit() and s.startswith("7656")

    def _cached_persona_name_from_disk(self, sid) -> str:
        """从内存状态/本地状态文件取上次已知的 Steam 昵称。"""
        sid = str(sid)
        for gid_states in (getattr(self, "group_last_states", {}) or {}).values():
            if not isinstance(gid_states, dict):
                continue
            st = gid_states.get(sid) or {}
            name = str(st.get("name") or "").strip()
            if name and name != sid and not self._is_steamid_like(name):
                return name
        try:
            data_dir = getattr(self, "data_dir", "") or ""
            if data_dir:
                import glob
                for fp in glob.glob(os.path.join(data_dir, "group_*_states.json")):
                    try:
                        with open(fp, encoding="utf-8-sig") as f:
                            data = json.load(f) or {}
                    except Exception:
                        continue
                    st = data.get(sid) or {}
                    name = str((st or {}).get("name") or "").strip()
                    if name and name != sid and not self._is_steamid_like(name):
                        return name
        except Exception:
            pass
        try:
            data_dir = getattr(self, "data_dir", "") or ""
            if data_dir:
                from .infrastructure.persistence import local_store as ls
                conn = ls.get_store(data_dir)
                row = conn.execute("SELECT nickname FROM bind_data WHERE sid=?", (sid,)).fetchone()
                if row:
                    nick = str(row["nickname"] or "").strip()
                    if nick and nick != "*":
                        return nick
        except Exception:
            pass
        return ""

    def _resolve_player_display_name(self, sid):
        """玩家显示名：绑定备注 > Steam昵称/状态缓存 > SteamID"""
        sid = str(sid)
        name = self._resolve_bind_name(sid, "")
        if name and name != sid and not self._is_steamid_like(name):
            return name
        cached = None
        try:
            cached = self._cached_persona_name(sid)
        except Exception:
            cached = None
        if cached and not self._is_steamid_like(cached):
            return cached
        for gid_states in (getattr(self, "group_last_states", {}) or {}).values():
            state = (gid_states or {}).get(sid, {}) or {}
            nm = str(state.get("name") or "").strip()
            if nm and nm != sid and not self._is_steamid_like(nm):
                return nm
        return sid

    # ========== 愿望单/打折已拆至 application/services/game_wishlist_service.py ==========

    # --- 指令入口（实现见 WishlistServiceMixin） ---
    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game wish")
    async def game_wish(self, event: AstrMessageEvent, target: str = ""):
        '''公开愿望单（全量分页，每页20条+封面）：/game wish @某人'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        try:
            await self._price_ack(event, "正在查询愿望单，请稍候…")
        except Exception:
            pass
        async for _r in self._game_wish_impl(event, target):
            yield _r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game wish_sale")
    async def game_wish_sale(self, event: AstrMessageEvent, action: str = "status"):
        '''愿望单打折推送：on|off|status|test [@某人|SteamID]'''
        async for _r in self._game_wish_sale_impl(event, action):
            yield _r



    def crop_image_auto(self, img_path_or_bytes, bg_color=(20,26,33), threshold=25):
        """
        自动裁剪图片内容区域，去除边缘与 bg_color 相近的空白。
        支持本地路径、bytes、URL、PIL.Image。
        """
        import numpy as np
        # 新增：如果已经是PIL.Image对象，直接用
        if isinstance(img_path_or_bytes, PILImage.Image):
            img = img_path_or_bytes.convert("RGB")
        elif isinstance(img_path_or_bytes, str) and (img_path_or_bytes.startswith("http://") or img_path_or_bytes.startswith("https://")):
            resp = requests.get(img_path_or_bytes, timeout=15, verify=requests_verify())
            resp.raise_for_status()
            img = PILImage.open(io.BytesIO(resp.content)).convert("RGB")
        elif isinstance(img_path_or_bytes, bytes):
            img = PILImage.open(io.BytesIO(img_path_or_bytes)).convert("RGB")
        else:
            img = PILImage.open(img_path_or_bytes).convert("RGB")
        arr = np.array(img)
        # 自动检测背景色（取四角平均色）
        h, w, _ = arr.shape
        corners = [arr[0,0], arr[0,-1], arr[-1,0], arr[-1,-1]]
        avg_bg = np.mean(corners, axis=0)
        # 计算每个像素与背景色的距离
        diff = np.abs(arr - avg_bg).sum(axis=2)
        mask = diff > threshold
        coords = np.argwhere(mask)
        if coords.size == 0:
            return img
        y0, x0 = coords.min(axis=0)
        y1, x1 = coords.max(axis=0) + 1
        # 防止裁剪过度，留出2px边距
        y0 = max(y0 - 0, 0)
        x0 = max(x0 - 0, 0)
        y1 = min(y1 - 0, arr.shape[0])
        x1 = min(x1 - 0, arr.shape[1])
        cropped = img.crop((x0, y0, x1, y1))
        return cropped


    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam on")
    async def steam_on(self, event: AstrMessageEvent):
        '''手动启动Steam状态监控轮询（分群）'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not is_valid_group_id(group_id):
            yield event.plain_result("请在群聊中使用该命令，私聊无法启动群监控。")
            return
        self.group_monitor_enabled[group_id] = True
        self._save_group_switches()
        if not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        steam_ids = self.group_steam_ids.get(group_id, [])
        if not steam_ids or not any(isinstance(x, str) and x.strip() for x in steam_ids):
            yield event.plain_result(
                "未设置监控的 SteamID 列表，请先在插件配置中填写 steam_ids，"
                "或使用 /steam addid [SteamID] 添加要监控的玩家。"
            )
            return
        if group_id in self.running_groups:
            yield event.plain_result("本群Steam监控已在运行。")
            return
        self.running_groups.add(group_id)
        if not hasattr(self, 'notify_sessions'):
            self.notify_sessions = {}
        self.notify_sessions[group_id] = event.unified_msg_origin
        self._record_platform_id(event)
        self._save_notify_session()
        # 初始化状态
        if group_id not in self.group_last_states:
            self.group_last_states[group_id] = {}
        # 批量查询所有玩家状态，减少API调用
        status_map = await self.fetch_player_statuses_batch(steam_ids) if steam_ids else {}
        now = int(time.time())
        for sid in steam_ids:
            status = status_map.get(sid)
            if not status:
                continue
            self.group_last_states[group_id][sid] = status
            await self.session_service.handle(
                group_id,
                sid,
                status.get('gameid'),
                now,
                player_name=status.get('name') or sid,
                current_game_name=status.get('gameextrainfo') or '未知游戏',
                status=status,
                skip_push=True,
            )
        yield event.plain_result("本群Steam状态监控启动完成喔！ヾ(≧ω≦)ゞ")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam addid")
    def _extract_at_qq_from_event(self, event) -> str:
        """从事件消息链提取第一个被 @ 的 QQ 号。

        AstrBot 有时把 at 段渲染成文本（如「昵称(QQ)」）塞进 nickname 参数，
        导致 at_user 为空；直接读消息链里的 At 组件最可靠。
        判断以 qq 字段为准（ComponentType 是枚举，str() 结果不可靠）。
        """
        try:
            msg_obj = getattr(event, "message_obj", None)
            segments = []
            if msg_obj is not None:
                segments = (getattr(msg_obj, "message", None)
                            or getattr(msg_obj, "message_chain", None) or [])
            _dbg = []
            for seg in segments or []:
                try:
                    qq = getattr(seg, "qq", None)
                    seg_type = getattr(seg, "type", None)
                    type_raw = getattr(seg_type, "value", seg_type)
                    type_l = str(type_raw or "").lower()
                    if isinstance(seg, dict):
                        seg_type = seg.get("type") or seg.get("msg_type") or seg_type
                        type_l = str(getattr(seg_type, "value", seg_type) or "").lower()
                        if qq is None:
                            qq = seg.get("qq") or (seg.get("data") or {}).get("qq")
                    if qq is None and "cq:at" in str(seg).lower():
                        _m = re.search(r"qq=(\d+)", str(seg))
                        qq = _m.group(1) if _m else None
                    if qq is None:
                        _dbg.append(type_l or type(seg).__name__)
                        continue
                    if str(qq) not in ("all", "0", ""):
                        return str(qq)
                except Exception:
                    continue
            if _dbg:
                logger.info(f"[at] 未取到@目标，消息段类型={_dbg}")
        except Exception:
            pass
        return ""

    async def _resolve_qq_by_nickname(self, event, name: str) -> str:
        """按群昵称反查 QQ（用于手打 @名字；名字含空格时 AstrBot 参数会被拆开）。

        仅在唯一匹配时返回，避免误绑定。结果缓存 120 秒。
        """
        name = (name or "").strip().lstrip("@").strip()
        # 去掉 AstrBot 渲染出的「昵称(QQ)」后缀
        name = re.sub(r"\s*\(\s*\d{5,}\s*\)\s*$", "", name).strip()
        if not name or name.isdigit() or len(name) < 2:
            return ""
        try:
            gid = str(event.get_group_id() or "")
            if not gid:
                return ""
            cache = getattr(self, "_nick_qq_cache", None)
            if cache is None:
                cache = {}
                self._nick_qq_cache = cache
            ck = f"{gid}:{name}"
            hit = cache.get(ck)
            now = time.time()
            if hit and now - hit[0] < 120:
                return hit[1]
            bot = getattr(event, "bot", None)
            if bot is None:
                return ""
            members = await bot.call_action("get_group_member_list", group_id=int(gid)) or []
            exact, partial = [], []
            for m in members:
                try:
                    card = str(m.get("card") or "")
                    nick = str(m.get("nickname") or "")
                    uid = str(m.get("user_id") or "")
                    if not uid:
                        continue
                    if name == card or name == nick:
                        exact.append(uid)
                    elif name in card or name in nick:
                        partial.append(uid)
                except Exception:
                    continue
            pick = exact[0] if len(exact) == 1 else (partial[0] if (not exact and len(partial) == 1) else "")
            cache[ck] = (now, pick)
            if pick:
                logger.info(f"[at] 昵称反查成功 name={name!r} -> {pick}")
            return pick
        except Exception as e:
            logger.info(f"[at] 昵称反查失败 name={name!r}: {format_exception(e)}")
            return ""

    async def steam_addid(self, event: AstrMessageEvent, steamid: str, at_user: str = "", nickname: str = ""):
        '''添加玩家到本群监控列表（分群），支持逗号分隔多个ID。
        Steam 支持 SteamID/个人资料链接/自定义ID/好友码；
        多平台使用前缀：psn:<在线ID> / xbox:<Gamertag|XUID>
        末尾可加 @用户 [备注名] 绑定QQ与玩家ID'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        if not is_valid_group_id(group_id):
            yield event.plain_result("请在群聊中使用该命令，或到 WebUI 填写有效群号后再添加。")
            return
        # 解析 @用户 [备注名] 后缀（多参数接收，兼容 AstrBot 参数分割）
        bind_qq = None
        bind_nickname = None
        # 优先从事件消息链取 @ 目标（at_user 常为空，at 被渲染进 nickname）
        _at_ev = self._extract_at_qq_from_event(event)
        if _at_ev:
            bind_qq = _at_ev
        # 兜底：第二个参数直接给纯数字 QQ 号（手打 @ 未生成真实 at 组件时可用）
        if not bind_qq and at_user and str(at_user).strip().isdigit() and len(str(at_user).strip()) >= 5:
            bind_qq = str(at_user).strip()
        if at_user:
            _m_at = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)|[^\s()]{1,64}\((\d{5,})\)', at_user.strip())
            _qq_at = (_m_at.group(1) or _m_at.group(2) or _m_at.group(3) or _m_at.group(4) or _m_at.group(5)) if _m_at else None
            # 只有解析成功才覆盖（否则会抹掉已从事件消息链取到的 QQ）
            if _qq_at:
                bind_qq = _qq_at
        if nickname:
            bind_nickname = nickname.strip()
        # 全部解析失败（手打 @名字/名字含空格被拆开）→ 按群昵称反查 QQ
        if not bind_qq:
            _names = [x for x in (str(at_user or "").strip(), str(nickname or "").strip()) if x]
            for _nm in _names:
                _qq_nick = await self._resolve_qq_by_nickname(event, _nm)
                if _qq_nick:
                    bind_qq = _qq_nick
                    break
        # 若 nickname 只是 at 的渲染文本（形如 昵称(QQ)，QQ 与本次绑定一致），视为噪声丢弃
        if bind_nickname and bind_qq:
            if re.fullmatch(r".{0,64}\(\s*" + re.escape(str(bind_qq)) + r"\s*\)", bind_nickname):
                bind_nickname = None
        # 仅以中英文逗号分隔多个 ID
        import re as _re
        raw_list = [x.strip() for x in _re.split(r'[,，]+', steamid) if x.strip()]
        # 逐个解析：带平台前缀（psn:/xbox:/nso:）直接保留，否则按 Steam 多种格式解析
        resolved_list = []
        invalid_list = []
        for raw in raw_list:
            sp = split_platform_sid(raw)
            if sp:
                if sp[0] == "nso":
                    yield event.plain_result(
                        "Nintendo/NSO 监控已暂停：任天堂 NSO API 暂不可用，无法获取在线状态。\n"
                        "可继续使用 Steam / PSN / Xbox。"
                    )
                    return
                resolved_list.append(raw)
                continue
            sid = await self.resolve_steam_input(raw)
            if sid and sid.isdigit() and len(sid) == 17:
                resolved_list.append(sid)
            else:
                invalid_list.append(raw)
        if invalid_list:
            yield event.plain_result(
                f"以下输入无法解析为有效ID：{', '.join(invalid_list)}\n"
                f"支持格式：SteamID64 / Steam个人资料链接 / Steam自定义ID / 8位好友码 / psn:<在线ID> / xbox:<Gamertag|XUID>"
            )
            return
        # 去重
        seen = set()
        steamid_list = []
        for sid in resolved_list:
            if sid not in seen:
                seen.add(sid)
                steamid_list.append(sid)
        steam_ids = self.group_steam_ids.setdefault(group_id, [])
        added = []
        pushed = []
        already = []
        already_pushed = []
        binding_updated = []
        pushed_primary_groups = {}
        limit = self.max_group_size
        for sid in steamid_list:
            if sid in steam_ids:
                already.append(sid)
                # 已在本群监控：若本次携带绑定/备注，仍允许更新（不直接跳过）
                if bind_qq or bind_nickname:
                    binding_updated.append(sid)
                continue
            primary_group = next(
                (
                    candidate
                    for candidate, candidate_ids in self.group_steam_ids.items()
                    if candidate != group_id and sid in candidate_ids
                ),
                None,
            )
            if primary_group is not None:
                targets = self.push_groups.setdefault(sid, [])
                pushed_primary_groups[sid] = primary_group
                if group_id in targets:
                    already_pushed.append(sid)
                else:
                    targets.append(group_id)
                    pushed.append(sid)
                continue
            if len(steam_ids) < limit:
                steam_ids.append(sid)
                added.append(sid)
            else:
                break
        self.group_steam_ids[group_id] = steam_ids
        if added:
            self._save_group_steam_ids()
        if pushed:
            self._save_push_groups()
        def _fmt_sid(s):
            sp = split_platform_sid(str(s))
            if not sp:
                return f"SteamID {s}"
            label = {"psn": "PSN", "xbox": "Xbox", "nso": "Nintendo"}.get(sp[0], sp[0])
            return f"{label} {sp[1]}"

        def _fmt_list(sids):
            return "、".join(_fmt_sid(s) for s in sids)

        # 绑定数据：写入并保存（已在本群监控的ID同样允许更新备注/绑定）
        if steamid_list and (bind_qq or bind_nickname):
            if not hasattr(self, '_bind_data'):
                self._bind_data = {}
            for sid in steamid_list:
                if bind_qq:
                    key = str(bind_qq)
                    prev = self._bind_data.get(key) or {}
                    # 一个 QQ 可绑多个平台号（Steam + PSN + Xbox）
                    sids = list(self._bind_info_sids(prev))
                    if str(sid) not in sids:
                        sids.append(str(sid))
                    nick = bind_nickname if bind_nickname else (prev.get("nickname") if prev.get("nickname") not in (None, "") else "*")
                    self._bind_data[key] = {"sids": sids, "nickname": nick}
                elif bind_nickname:
                    matched = False
                    for _key, _info in list(self._bind_data.items()):
                        if str(sid) in self._bind_info_sids(_info):
                            self._bind_data[_key]["nickname"] = bind_nickname
                            matched = True
                    if not matched:
                        self._bind_data[f"__remark:{sid}"] = {"sids": [str(sid)], "nickname": bind_nickname}
            self._save_bind_data()
            logger.info(f"[绑定] {'QQ'+str(bind_qq) if bind_qq else '备注'} -> 玩家 {steamid_list[-1]}，备注={bind_nickname or '无'}")
        msg = ""
        if added:
            # 按平台显示添加提示（Steam 保持原文案；多平台按前缀识别）
            _plat_label = {
                "psn": "PSN", "xbox": "Xbox", "nso": "Nintendo",
            }
            parts = []
            for sid in added:
                sp = split_platform_sid(str(sid))
                if sp:
                    label = _plat_label.get(sp[0], sp[0])
                    parts.append(f"{label}玩家: {sp[1]}")
                else:
                    parts.append(f"SteamID: {sid}")
            if parts:
                msg += "已为本群添加" + ", ".join(parts) + "\n"
        if pushed:
            push_details = []
            for sid in pushed:
                primary_group = pushed_primary_groups.get(sid)
                suffix = f"（主监控群：{primary_group}）" if primary_group else ""
                push_details.append(f"{sid}{suffix}")
            msg += (
                "以下SteamID已被其他群监控，当前群不会重复监控，已自动设置为分发路由（push_group）："
                f"{', '.join(push_details)}\n"
            )
        if binding_updated:
            msg += f"以下玩家已在本群监控，备注/绑定已更新：{_fmt_list(binding_updated)}\n"
        already_plain = [sid for sid in already if sid not in binding_updated]
        if already_plain:
            msg += f"以下玩家已经在本群监控，无需重复添加：{_fmt_list(already_plain)}\n"
        if already_pushed:
            push_details = []
            for sid in already_pushed:
                primary_group = pushed_primary_groups.get(sid)
                suffix = f"（主监控群：{primary_group}）" if primary_group else ""
                push_details.append(f"{_fmt_sid(sid)}{suffix}")
            msg += f"以下玩家已经是本群的分发路由（push_group），无需重复添加：{'、'.join(push_details)}\n"
        unhandled = len(steamid_list) - len(added) - len(pushed) - len(already) - len(already_pushed)
        if unhandled:
            msg += f"本群监控组人数已达上限（{limit}人），部分ID未添加。\n"
        # 自动启用本群监控（幂等）
        if added and group_id not in self.running_groups:
            self.group_monitor_enabled[group_id] = True
            self.running_groups.add(group_id)
            if not hasattr(self, 'notify_sessions'):
                self.notify_sessions = {}
            self.notify_sessions[group_id] = event.unified_msg_origin
            self._record_platform_id(event)
            self._save_notify_session()
            if group_id not in self.group_last_states:
                self.group_last_states[group_id] = {}
            msg += "监控已自动启动。\n"
        yield event.plain_result(msg.strip() if msg else "未添加任何玩家。")

    # ---------- 新指令：/game 平台分类 ----------

    async def _game_resolve_add_id(self, platform: str, raw: str):
        """把 /game <plat> add 的参数解析成内部 sid。
        platform: steam | ps/psn | xbox
        返回 (sid, error_msg)
        """
        raw = (raw or "").strip()
        if not raw:
            return None, "缺少玩家 ID"
        plat = (platform or "").strip().lower()
        if plat in ("steam", "s"):
            # 已带平台前缀则原样
            if split_platform_sid(raw):
                if raw.lower().startswith("nso:"):
                    return None, "Nintendo/NSO 监控已暂停"
                return raw, None
            sid = await self.resolve_steam_input(raw)
            if sid and sid.isdigit() and len(sid) == 17:
                return sid, None
            return None, f"无法解析 Steam ID：{raw}"
        if plat in ("ps", "psn", "playstation"):
            if raw.lower().startswith("psn:"):
                return raw, None
            return f"psn:{raw}", None
        if plat in ("xbox", "xb", "x"):
            if raw.lower().startswith("xbox:"):
                return raw, None
            return f"xbox:{raw}", None
        if plat in ("nso", "ns", "switch", "nintendo"):
            return None, "Nintendo/NSO 监控已暂停：上游 API 不可用"
        return None, f"未知平台：{platform}"

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game steam add")
    async def game_steam_add(self, event: AstrMessageEvent, steamid: str, at_user: str = "", nickname: str = ""):
        '''添加 Steam 玩家：/game steam add <SteamID/链接/好友码> [@QQ] [备注]'''
        sid, err = await self._game_resolve_add_id("steam", steamid)
        if err:
            yield event.plain_result(err)
            return
        async for r in self.steam_addid(event, sid, at_user, nickname):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game ps add")
    async def game_ps_add(self, event: AstrMessageEvent, online_id: str, at_user: str = "", nickname: str = ""):
        '''添加 PSN 玩家：/game ps add <在线ID> [@QQ] [备注]'''
        sid, err = await self._game_resolve_add_id("ps", online_id)
        if err:
            yield event.plain_result(err)
            return
        async for r in self.steam_addid(event, sid, at_user, nickname):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game psn add")
    async def game_psn_add_alias(self, event: AstrMessageEvent, online_id: str, at_user: str = "", nickname: str = ""):
        '''/game ps add 的别名'''
        async for r in self.game_ps_add(event, online_id, at_user, nickname):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game xbox add")
    async def game_xbox_add(self, event: AstrMessageEvent, gamertag: str, at_user: str = "", nickname: str = ""):
        '''添加 Xbox 玩家：/game xbox add <Gamertag|XUID> [@QQ] [备注]'''
        sid, err = await self._game_resolve_add_id("xbox", gamertag)
        if err:
            yield event.plain_result(err)
            return
        async for r in self.steam_addid(event, sid, at_user, nickname):
            yield r

    async def _game_del(self, event: AstrMessageEvent, platform: str, raw_id: str, group_id_param: str = ""):
        sid, err = await self._game_resolve_add_id(platform, raw_id)
        if err:
            yield event.plain_result(err)
            return
        # 多平台：直接按完整 sid 删除
        sp = split_platform_sid(sid)
        if sp:
            group_id = group_id_param.strip() if group_id_param.strip() else (
                str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
            )
            from ..application.services.monitor_admin import MonitorAdminService
            result = MonitorAdminService(self).remove_player(group_id, sid)
            if not result.changed:
                yield event.plain_result(f"该玩家不存在于群 {group_id} 的监控组: {sid}")
                return
            yield event.plain_result(f"已删除 {sid}（群 {group_id}）")
            return
        async for r in self.steam_delid(event, sid, group_id_param):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game steam del")
    async def game_steam_del(self, event: AstrMessageEvent, steamid: str, group_id_param: str = ""):
        '''删除 Steam 玩家：/game steam del <SteamID> [群号]'''
        async for r in self._game_del(event, "steam", steamid, group_id_param):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game ps del")
    async def game_ps_del(self, event: AstrMessageEvent, online_id: str, group_id_param: str = ""):
        '''删除 PSN 玩家：/game ps del <在线ID> [群号]'''
        async for r in self._game_del(event, "ps", online_id, group_id_param):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game psn del")
    async def game_psn_del_alias(self, event: AstrMessageEvent, online_id: str, group_id_param: str = ""):
        async for r in self.game_ps_del(event, online_id, group_id_param):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game xbox del")
    async def game_xbox_del(self, event: AstrMessageEvent, gamertag: str, group_id_param: str = ""):
        '''删除 Xbox 玩家：/game xbox del <Gamertag|XUID> [群号]'''
        async for r in self._game_del(event, "xbox", gamertag, group_id_param):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game on")
    async def game_on(self, event: AstrMessageEvent):
        '''开启本群监控'''
        async for r in self.steam_on(event):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("game off")
    async def game_off(self, event: AstrMessageEvent):
        '''关闭本群监控'''
        async for r in self.steam_off(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game all")
    async def game_all(self, event: AstrMessageEvent, mode: str = "img"):
        '''全群玩家总览'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        async for r in self.steam_alllist(event, mode):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game who")
    async def game_who(self, event: AstrMessageEvent, qq: str = ""):
        '''查询绑定玩家状态'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        async for r in self.steam_who(event, qq):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game help")
    async def game_help(self, event: AstrMessageEvent):
        '''帮助'''
        async for r in self.steam_help(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game activity")
    async def game_activity(self, event: AstrMessageEvent, target: str = "", days: str = ""):
        '''查看购游戏日志：/game activity [天数] [@某人]'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        qq_clean = None
        n_days = 7
        for part in (target, days):
            if not part or not str(part).strip():
                continue
            part = str(part).strip()
            if part.isdigit():
                n_days = max(1, min(90, int(part)))
                continue
            m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', part)
            if m and not qq_clean:
                qq_clean = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        # 日期筛选
        from datetime import timedelta as _td
        cutoff = (date.today() - _td(days=n_days - 1)).isoformat()
        if qq_clean:
            bind_sids = set(self._bind_sids_for_qq(qq_clean))
            logs = [x for x in self.owned_games_log if x.get("sid") in bind_sids and x.get("date", "") >= cutoff]
            title = f"QQ {qq_clean} 购游戏记录"
        else:
            group_sids = set(str(s) for s in self.group_steam_ids.get(group_id, []))
            logs = [x for x in self.owned_games_log if x.get("sid") in group_sids and x.get("date", "") >= cutoff]
            title = "本群购游戏日志"
        period = f"今日" if n_days == 1 else f"最近{n_days}天"
        if not logs:
            yield event.plain_result(f"{title}（{period}）：暂无记录。")
            return
        logs = logs[-15:]
        items = []
        for item in reversed(logs):
            sid = item.get("sid", "")
            items.append({
                "name": self._resolve_player_display_name(sid),
                "game_name": item.get("game_name", "?"),
                "appid": item.get("appid", ""),
                "date": item.get("date", ""),
            })
        # 并发拉封面
        from ..presentation.renderers.activity import render_activity_image
        covers = {}
        for it in items:
            appid = str(it.get("appid") or "")
            if appid:
                cp = await self.get_game_cover_url(appid)
                if cp:
                    covers[appid] = cp
        img_bytes = await render_activity_image(
            f"{title} · {period}", items,
            font_path=self.get_font_path("NotoSansHans-Regular.otf"),
            covers=covers, proxy=self.proxy,
        )
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game lib")

    async def game_lib(self, event: AstrMessageEvent, target: str = ""):
        '''查看 Steam 游戏库（按总时长排序）：/game lib @某人 或 /game lib'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        qq_clean = None
        if target:
            m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', target.strip())
            if m:
                qq_clean = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if qq_clean:
            sid = None
            for s in self._bind_sids_for_qq(qq_clean):
                if str(s).isdigit() and len(str(s)) == 17:
                    sid = str(s)
                    break
            if not sid:
                yield event.plain_result(f"QQ {qq_clean} 未绑定 Steam 玩家。")
                return
            sids = [sid]
        else:
            # 取本群第一个 Steam 玩家（如果只有一个）；多个则取第一个
            sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
            if not sids:
                yield event.plain_result("本群没有 Steam 玩家。")
                return
            if len(sids) > 1:
                yield event.plain_result(f"本群有 {len(sids)} 个 Steam 玩家，请 @ 指定：/game lib @某人")
                return
        sid = sids[0]
        yield event.plain_result("正在获取游戏库，请稍候...")
        games = await self.fetch_owned_games(sid)
        if games is None:
            yield event.plain_result("获取失败：请确认 Steam API Key 有效，且玩家资料「游戏详情」已公开。")
            return
        if not games:
            yield event.plain_result("该玩家游戏库为空或已设为私密。")
            return
        player_name = self._resolve_bind_name(sid, sid)
        from ..presentation.renderers.game_lib import render_game_lib_image
        img_bytes = await render_game_lib_image(
            self.data_dir, player_name, games,
            font_path=self.get_font_path("NotoSansHans-Regular.otf"),
            proxy=self.proxy, limit=15,
        )
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game coop")
    async def game_coop(self, event: AstrMessageEvent, target: str = ""):
        '''开黑雷达：查多人共有游戏。/game coop @A @B 或 /game coop（本群全部 Steam 玩家）'''
        from ..presentation.renderers.game_coop import (
            collect_coop_targets,
            find_common_games,
            render_coop_card,
        )

        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sids, unbound, qq_list = collect_coop_targets(self, event, target=target, group_id=group_id)
        logger.info(f"[coop] qq={qq_list} sids={sids} unbound={unbound}")

        if qq_list:
            if len(sids) < 2:
                parts = []
                if unbound:
                    parts.append(f"未绑定 Steam：{'、'.join(unbound)}")
                if sids:
                    parts.append(f"已解析 {len(sids)} 人")
                hint = f"（{'；'.join(parts)}）" if parts else ""
                yield event.plain_result(
                    f"至少需要 2 位已绑定 Steam 的玩家{hint}。\n"
                    f"用法：/game coop @A @B\n"
                    f"绑定：/game steam add <SteamID> @某人"
                )
                return
        else:
            sids = [str(s) for s in self.group_steam_ids.get(group_id, []) if str(s).isdigit() and len(str(s)) == 17]
            if len(sids) < 2:
                yield event.plain_result("本群 Steam 玩家不足 2 人，或请 @ 指定：/game coop @A @B")
                return

        yield event.plain_result(f"正在分析 {len(sids)} 位玩家的游戏库，稍候...")
        result = await find_common_games(self, sids, proxy=self.proxy, only_multiplayer=True)
        games = result.get("games") or []
        if not games:
            fail_n = len(result.get("failed_sids") or [])
            extra = f"（{fail_n} 人库不可见/获取失败）" if fail_n else ""
            yield event.plain_result(f"没有找到共同游戏{extra}。请确认资料「游戏详情」已公开。")
            return
        img_bytes = render_coop_card(
            result.get("players") or [],
            games,
            font_path=self.get_font_path("NotoSansHans-Regular.otf"),
            filtered=result.get("filtered", True),
            note="库缓存约 6 小时；筛选失败时可能含单机",
        )
        if not img_bytes:
            yield event.plain_result("渲染失败")
            return
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(img_bytes)
            yield event.image_result(tmp.name)


    # ========== 全成就已拆至 application/services/perfect_games_service.py ==========


    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game ach")
    async def game_ach(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        '''查全成就游戏：/game ach [@某人]  停止扫描：/game ach stop
        查某游戏成就列表：/game ach <appid> [@某人]'''
        try:
            await self._price_ack(event, "正在查询成就/全成就，请稍候…")
        except Exception:
            pass
        try:
            async for _r in self._game_ach_impl(event, arg1, arg2):
                yield _r
        except Exception as e:
            logger.exception(f"[game_ach] 指令执行异常: {e}")
            try:
                yield event.plain_result(
                    f"执行 /game ach 失败：{e}\n"
                    f"可再试一次，或发 /game ach stop 释放扫描锁。"
                )
            except Exception:
                pass




    # --- 查价短指令入口（实现见 GamePriceServiceMixin） ---
    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game price")
    async def game_price(self, event: AstrMessageEvent, query: str):
        async for r in self._game_price_impl(event, query):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("price")
    async def price_short(self, event: AstrMessageEvent, query: str = ""):
        '''价格查询：/price 游戏名'''
        async for r in self._price_short_impl(event, query):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("px")
    async def px_short(self, event: AstrMessageEvent, query: str = ""):
        '''价格快捷查询：/px 游戏名（直接返回第一条匹配）'''
        async for r in self._px_short_impl(event, query):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("rank")
    async def rank_short(self, event: AstrMessageEvent, period: str = ""):
        '''本群排行榜：/rank [天数]'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        async for r in self.steam_rank(event, period):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delid")
    async def steam_delid(self, event: AstrMessageEvent, steamid: str, group_id_param: str = ""):
        '''从监控组删除玩家；支持 Steam 好友码/链接 或 psn:xxx / xbox:xxx'''
        group_id = group_id_param.strip() if group_id_param.strip() else (str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default')
        sp = split_platform_sid(str(steamid).strip())
        if sp:
            sid = str(steamid).strip()
        else:
            sid = await self.resolve_steam_input(steamid)
            if not sid or not sid.isdigit() or len(sid) != 17:
                yield event.plain_result("无法解析为有效ID，支持：SteamID64/链接/好友码 或 psn:xxx / xbox:xxx")
                return
        from ..application.services.monitor_admin import MonitorAdminService

        result = MonitorAdminService(self).remove_player(group_id, sid)
        if not result.changed:
            yield event.plain_result(
                f"该玩家不存在于群 {group_id} 的监控组或分发路由: {sid}"
            )
            return

        if result.message == "removed push route":
            yield event.plain_result(f"已关闭群 {group_id} 对 {sid} 的分发路由")
        else:
            yield event.plain_result(
                f"已删除 {sid} 的主监控及全部路由分发"
            )

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam game")
    async def steam_game(self, event: AstrMessageEvent, appid: str):
        """查询 Steam 游戏详情并生成详情卡片。"""
        appid = str(appid).strip()
        if not appid.isdigit():
            yield event.plain_result("用法：/steam game <Steam AppID>")
            return
        game = await self.fetch_game_details(appid)
        if not game:
            yield event.plain_result(f"未找到 Steam 游戏 AppID：{appid}，或 Steam 商店暂时无法访问。")
            return
        try:
            img_bytes = await render_game_detail_image(
                game,
                font_path=get_font_path("NotoSansHans-Regular.otf"),
                proxy=self.proxy,
            )
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                image_path = tmp.name
            yield event.image_result(image_path)
        except Exception as exc:
            logger.exception("渲染 Steam 游戏详情卡片失败: %s", exc)
            yield event.plain_result(f"游戏详情获取成功，但卡片生成失败：{exc}")


    # --- 查价实现入口（实现见 GamePriceServiceMixin） ---
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def steam_price_selection(self, event: AstrMessageEvent):
        async for r in self._steam_price_selection_impl(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam")
    async def steam_lookup(self, event: AstrMessageEvent, query: str = "", target: str = ""):
        async for r in self._steam_lookup_impl(event, query, target):
            yield r


    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam list")
    async def steam_list(self, event: AstrMessageEvent):
        '''列出本群 Steam 玩家状态'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        try:
            await self._price_ack(event, "正在拉取本群 Steam 玩家状态…")
        except Exception:
            pass
        async for result in self._game_list_platform(event, "steam"):
            yield result

    async def _probe_one(self, label: str, url: str, *, proxy=None, timeout=8.0, expect_json=False, params=None) -> str:
        """探测单个接口，返回一行状态（独立 client，不占共享池）。"""
        t0 = time.time()
        import httpx as _httpx
        from ..shared.network import httpx_client_kwargs as _kwargs

        try:
            kw = dict(_kwargs(proxy))
            kw["trust_env"] = False
            async with _httpx.AsyncClient(
                timeout=_httpx.Timeout(timeout, connect=5.0),
                follow_redirects=True,
                **kw,
            ) as client:
                r = await client.get(url, params=params or None)
            dt = int((time.time() - t0) * 1000)
            code = r.status_code
            # 探测结果写入商店全局冷却，避免 status 与业务感知不一致
            if code == 403 or (code and 200 <= code < 300):
                if "商店" in label or "store" in label.lower() or "页面" in label:
                    try:
                        note_steam_store_status(code, label=f"status:{label}")
                    except Exception:
                        pass
            if code == 200:
                extra = ""
                if expect_json:
                    try:
                        r.json()
                        extra = " json✓"
                    except Exception:
                        extra = " json✗"
                return f"✅ {label} HTTP200 {dt}ms{extra}"
            if code == 403:
                return f"❌ {label} HTTP403 限流 {dt}ms"
            return f"⚠ {label} HTTP{code} {dt}ms"
        except Exception as e:
            return f"❌ {label} {type(e).__name__}: {str(e)[:50]}"

    async def _steam_net_status_impl(self) -> str:
        """探测 Steam WebAPI / 商店 / ITAD / 代理 / 本地库是否可用。"""
        from ..infrastructure.persistence import local_store as lstore
        from ..infrastructure.clients.itad import ITADClient

        lines = []
        lines.append(f"⏱ 接口状态 {time.strftime('%m-%d %H:%M:%S')}")
        ban = steam_store_ban_remaining()
        streak = steam_store_403_streak()
        if ban > 0:
            lines.append(f"⛔ 商店全局冷却：约 {int((ban + 59) // 60)} 分钟（403 streak={streak}）")
        else:
            lines.append(f"✅ 商店全局冷却：无（403 streak={streak}）")

        px = getattr(self, "proxy", None) or None
        px_label = px or "直连"
        lines.append(f"🔌 代理配置：{px_label}")

        api_key = (getattr(self, "API_KEY", "") or "").strip()
        base = (getattr(self, "STEAM_API_BASE", None) or "https://api.steampowered.com").rstrip("/")
        store = (getattr(self, "STEAM_STORE_BASE", None) or "https://store.steampowered.com").rstrip("/")

        # 1) Steam WebAPI（状态走专用池；与状态池同策略：有代理优先代理）
        if api_key:
            url = f"{base}/ISteamUser/GetPlayerSummaries/v0002/?key={api_key}&steamids=0"
            lines.append(await self._probe_one("Steam WebAPI(状态)", url, proxy=px, expect_json=True))
        else:
            lines.append("⚠ Steam WebAPI：未配置 API Key，跳过探测")
        # GetItems 常用于元数据
        import json as _json
        getitems_params = None
        if api_key:
            getitems_params = {
                "input_json": _json.dumps({"ids": [{"appid": 730}]}, separators=(",", ":")),
                "key": api_key,
            }
        lines.append(await self._probe_one(
            "Steam GetItems(元数据)",
            f"{base}/IStoreBrowseService/GetItems/v1/",
            proxy=px,
            expect_json=bool(api_key),
            params=getitems_params,
        ))

        # 2) Steam 商店（查价/搜索/愿望单折扣）
        lines.append(await self._probe_one(
            "Steam 商店 appdetails",
            f"{store}/api/appdetails",
            proxy=px,
            timeout=10.0,
            expect_json=True,
            params={"appids": "730", "cc": "cn", "l": "schinese"},
        ))
        lines.append(await self._probe_one(
            "Steam 商店 storesearch",
            f"{store}/api/storesearch/",
            proxy=px,
            timeout=10.0,
            expect_json=True,
            params={"term": "test", "l": "english", "cc": "us"},
        ))
        lines.append(await self._probe_one(
            "Steam 商店页面(app)",
            f"{store}/app/730/",
            proxy=px,
            timeout=10.0,
            params={"cc": "cn", "l": "schinese"},
        ))

        # 3) ITAD
        itad = getattr(self, "ITAD_CLIENT", None)
        itad_key = (getattr(itad, "api_key", "") or "").strip() if itad else ""
        itad_base = (getattr(itad, "base_url", None) or "https://api.isthereanydeal.com").rstrip("/") if itad else "https://api.isthereanydeal.com"
        if itad_key:
            lines.append(await self._probe_one(
                "ITAD 搜索(史低)",
                f"{itad_base}/games/search/v1",
                proxy=getattr(itad, "proxy", None) if itad else None,
                expect_json=True,
                params={"key": itad_key, "title": "test", "results": 1},
            ))
        else:
            lines.append("⚠ ITAD：未配置 Key")

        # 4) 代理连通性（商店走代理时是否可用）
        if px:
            lines.append(await self._probe_one(
                "代理→商店域名",
                f"{store}/",
                proxy=px,
                timeout=8.0,
            ))
        else:
            lines.append("ℹ 代理：未启用（全部直连）")

        # 5) 本地库
        try:
            conn = lstore.get_store(getattr(self, "data_dir", "") or "")
            st = lstore.stats(conn)
            typed = conn.execute(
                "SELECT COUNT(*) FROM games WHERE content_type IS NOT NULL AND content_type!=''"
            ).fetchone()[0]
            parents = conn.execute(
                "SELECT COUNT(*) FROM games WHERE parent_appid IS NOT NULL AND parent_appid!=''"
            ).fetchone()[0]
            path = lstore.db_path(getattr(self, "data_dir", "") or "")
            import os as _os
            size_mb = _os.path.getsize(path) / 1024 / 1024 if _os.path.exists(path) else 0
            lines.append(
                f"💾 本地库：games={st.get('games', 0)} 已标type={typed} 有parent={parents} "
                f"价格={st.get('prices', 0)} 史低={st.get('price_history', 0)} {size_mb:.1f}MB"
            )
        except Exception as e:
            lines.append(f"❌ 本地库：{e}")

        # 6) 轮询 / 愿望单配置摘要
        try:
            fixed = int((self.config or {}).get("fixed_poll_interval", 0) or 0)
            smart = (self.config or {}).get("smart_poll_intervals", "")
            if fixed > 0:
                lines.append(f"🔁 状态轮询：固定 {fixed}s")
            else:
                lines.append(f"🔁 状态轮询：智能 {smart}")
            enabled = getattr(self, "wish_sale_enabled_groups", set()) or set()
            lines.append(f"🎁 wish_sale 群：{', '.join(str(g) for g in enabled) or '未开启'}")
            try:
                auto_pull = getattr(self, "_wish_sale_auto_pull_wishlist", lambda: False)()
                qleft = getattr(self, "_wish_quota_left", lambda: 0)()
                qlimit = getattr(self, "_wish_sale_hourly_limit", lambda: 200)()
                lines.append(
                    f"   列表={'自动' if auto_pull else '手动update'} · "
                    f"小时额度 {qlimit - qleft}/{qlimit} · "
                    f"每人每轮 {getattr(self, '_wish_sale_round_limit', lambda: 50)()} 条"
                )
            except Exception:
                pass
            wban = float(getattr(self, "_wish_store_ban_until", 0) or 0)
            if wban > time.time():
                lines.append(f"⛔ 愿望单 SSR 冷却：约 {int((wban - time.time()) // 60)} 分钟")
            else:
                lines.append("✅ 愿望单 SSR 冷却：无")
        except Exception:
            pass

        # 状态连接池可观测（in_flight=并发状态请求协程数，不是连接槽位）
        try:
            sp = status_pool_stats()
            last_to = ""
            if sp.get("last_timeout_ts"):
                last_to = time.strftime("%H:%M:%S", time.localtime(sp["last_timeout_ts"]))
            inflight = sp.get("in_flight", 0)
            lim = sp.get("semaphore_limit", 6)
            util = sp.get("utilization", 0)
            mark = "⛔" if sp.get("blocked") else ("🟡" if inflight >= lim else "✅")
            lines.append(
                f"{mark} 状态池：并发 {inflight}/{lim}（峰值 {sp.get('peak_in_flight', 0)}"
                f"，连接上限 {sp.get('max_connections', 0)}）"
                f" 成功 {sp.get('acquire_ok', 0)} 超时 {sp.get('pool_timeout', 0)}"
                + (f" 最近超时 {last_to}" if last_to else "")
            )
        except Exception as e:
            lines.append(f"⚠ 状态池指标读取失败: {e}")

        guard = steam_store_blocked()
        lines.append("——")
        if guard:
            lines.append("提示：商店冷却中，/price 搜索与折扣页会受限；WebAPI/ITAD 仍可能可用。")
        else:
            lines.append("说明：✅=通 ❌=断/403 ⚠=异常；商店冷却只影响 store 域名。")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("status")
    async def status_cmd(self, event: AstrMessageEvent):
        '''接口连通状态：/status'''
        try:
            await self._price_ack(event, "正在探测各接口，请稍候…")
        except Exception:
            pass
        try:
            text = await self._steam_net_status_impl()
        except Exception as e:
            logger.exception(f"[status] 状态探测失败: {e}")
            text = f"接口状态探测失败：{e}"
        yield event.plain_result(text)

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam status")
    async def steam_status_cmd(self, event: AstrMessageEvent):
        '''接口连通状态：/steam status'''
        async for r in self.status_cmd(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam net")
    async def steam_net(self, event: AstrMessageEvent):
        '''接口连通状态：/status 的别名'''
        async for r in self.status_cmd(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam netstatus")
    async def steam_netstatus(self, event: AstrMessageEvent):
        '''接口连通状态：/status 的别名'''
        async for r in self.status_cmd(event):
            yield r

    async def _game_list_platform(self, event, platform: str):
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        direct_steam_ids = self.group_steam_ids.get(group_id, [])
        push_steam_ids = [
            sid
            for sid, push_groups in (getattr(self, 'push_groups', {}) or {}).items()
            if group_id in {str(target) for target in push_groups}
        ]
        steam_ids = list(dict.fromkeys([*direct_steam_ids, *push_steam_ids]))
        if not steam_ids:
            yield event.plain_result("本群未设置监控玩家列表，请先添加。")
            return
        if platform == "steam" and not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        event.group_steam_ids = steam_ids
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        async for result in handle_steam_list(
            self, event, group_id=group_id, font_path=font_path, proxy=self.proxy, platform=platform,
        ):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game steam list")
    async def game_steam_list(self, event: AstrMessageEvent):
        '''本群 Steam 玩家状态'''
        async for result in self._game_list_platform(event, "steam"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game list")
    async def game_list(self, event: AstrMessageEvent):
        '''本群 Steam 玩家状态（game steam list 别名）'''
        async for result in self._game_list_platform(event, "steam"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game ps list")
    async def game_ps_list(self, event: AstrMessageEvent):
        '''本群 PSN 玩家状态'''
        async for result in self._game_list_platform(event, "psn"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game psn list")
    async def game_psn_list_alias(self, event: AstrMessageEvent):
        async for result in self._game_list_platform(event, "psn"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game xbox list")
    async def game_xbox_list(self, event: AstrMessageEvent):
        '''本群 Xbox 玩家状态'''
        async for result in self._game_list_platform(event, "xbox"):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam config")
    async def steam_config(self, event: AstrMessageEvent):
        '''显示当前插件配置（敏感信息已隐藏）'''
        lines = []
        hidden_keys = {"steam_api_key", "sgdb_api_key"}
        for k, v in self.config.items():
            if k in hidden_keys:
                lines.append(f"{k}: ****** (已隐藏)")
            else:
                lines.append(f"{k}: {v}")
        # 新增：显示智能轮询间隔说明
        if hasattr(self, "smart_poll_intervals"):
            intervals = self.smart_poll_intervals
            lines.append(f"智能轮询间隔（分钟）: {intervals}（依次为[游戏中, 12分钟内, 12分钟~3小时, 3小时~24小时, 24~48小时, 超过48小时]）")
        yield event.plain_result("当前配置：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam set")
    async def steam_set(self, event: AstrMessageEvent, key: str, value: str):
        '''设置配置参数，立即生效（如 steam set fixed_poll_interval 600）'''
        if key not in self.config:
            yield event.plain_result(f"无效参数: {key}")
            return
        old = self.config[key]
        if key == "smart_poll_intervals":
            # 支持字符串输入
            value_list = [int(x.strip()) for x in value.split(",") if x.strip()]
            value = ",".join(str(x) for x in value_list)
            self.smart_poll_intervals = value_list
        elif isinstance(old, int):
            try:
                value = int(value)
            except Exception:
                yield event.plain_result("类型错误，应为整数")
                return
        elif isinstance(old, float):
            try:
                value = float(value)
            except Exception:
                yield event.plain_result("类型错误，应为浮点数")
                return
        elif isinstance(old, list):
            # 兼容旧格式
            value = [int(x.strip()) for x in value.split(",") if x.strip()]
        self.config[key] = value
        # 同步到属性
        self.API_KEY = self.config.get('steam_api_key', '')
        self.STEAM_IDS = self.config.get('steam_ids', [])
        self.RETRY_TIMES = self.config.get('retry_times', 3)
        self.GROUP_ID = self.config.get('notify_group_id', None)
        self.fixed_poll_interval = self.config.get('fixed_poll_interval', 0)
        # 重新解析智能轮询间隔
        raw_intervals = self.config.get('smart_poll_intervals', "1,3,5,10,20,30")
        if isinstance(raw_intervals, str):
            self.smart_poll_intervals = [int(x.strip()) for x in raw_intervals.split(",") if x.strip()]
        else:
            self.smart_poll_intervals = list(raw_intervals)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"已设置 {key} = {value}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rs")
    async def steam_rs(self, event: AstrMessageEvent):
        '''清除所有状态并初始化（重启插件用）'''
        self.group_last_states.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.playing_sessions.clear()
        getattr(self, "_session_meta", {}).clear()
        self.group_recent_games.clear()
        self._superpower_cache.clear()
        self._game_name_cache.clear()
        self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        self.running_groups.clear()
        self.group_monitor_enabled.clear()
        self.group_achievement_enabled.clear()
        self.notify_sessions = {}
        self._save_group_switches()
        self._save_persistent_data(force=True)  # 清空后保存
        yield event.plain_result("Steam状态监控插件已重置，所有状态已清空。")

    async def _render_daily_rank_file(self, rank_data, period_label="昨日", personal=False):
        """补齐排行榜展示信息并渲染为临时图片。"""
        sid_set = set()
        for player in rank_data:
            sid_set.add(player["sid"])
            for s in player.get("sids") or []:
                sid_set.add(s)
        sid_info = {}
        if sid_set:
            status_map = await self.fetch_player_statuses_batch(list(sid_set))
            for sid, info in status_map.items():
                sid_info[sid] = {
                    "name": info.get("name") or sid,
                    "avatar_url": info.get("avatarfull") or info.get("avatar"),
                }

        yesterday = self._get_day_key(-1)
        day_data = self.play_records.get(yesterday, {})
        for player in rank_data:
            sid = player["sid"]
            info = sid_info.get(sid, {})
            # 多平台 sid 不要用 sid[-8:]（xbox:EniVILL 会变成 ":EniVILL"）
            fallback_name = info.get("name")
            if not fallback_name:
                sp = split_platform_sid(str(sid))
                fallback_name = sp[1] if sp else str(sid)[-8:]
            player["name"] = self._resolve_bind_name(sid, fallback_name)
            player["avatar_url"] = info.get("avatar_url")
            player["top_game_id"] = player.get("top_game_id") or None
            if not player["games"]:
                continue
            # 优先用合并后 games[0]（时长最长）的 gameid
            if not player["top_game_id"]:
                player["top_game_id"] = player["games"][0].get("gameid")
            top_name = player["games"][0]["name"]
            if not player["top_game_id"]:
                for s in (player.get("sids") or [sid]):
                    for game_id, game_info in day_data.get(s, {}).items():
                        if game_info.get("name") == top_name:
                            player["top_game_id"] = game_id
                            break
                    if player["top_game_id"]:
                        break

        async def cover_fetcher(gameid):
            path = await self.get_game_cover_url(gameid)
            if path:
                return path
            # Xbox/PS 数字 titleId 不是 Steam appid：按游戏名解析封面
            try:
                name = None
                for p in rank_data:
                    for g in p.get("games") or []:
                        if str(g.get("gameid")) == str(gameid):
                            name = g.get("name")
                            break
                    if name:
                        break
                if name:
                    url = await self.resolve_cover_by_game_name(name)
                    if url:
                        from ..presentation.renderers.game_start import _download_cover
                        return await _download_cover(self.data_dir, gameid, url, proxy=self.proxy)
            except Exception as e:
                logger.debug(f"[排行榜] 名称封面失败 {gameid}: {e}")
            return None

        from ..presentation.renderers.render_assets import gather_avatar_frames
        frame_sids = []
        for player in rank_data:
            frame_sids.append(player.get("sid"))
            for s in player.get("sids") or []:
                frame_sids.append(s)
        avatar_frame_paths = await gather_avatar_frames(self.data_dir, frame_sids, proxy=self.proxy)

        # 排行榜游戏名统一转中文名来源（覆盖插件重启/缓存污染等写入的英文名）
        # 多平台 titleId 不是 Steam appid，跳过商店查询，避免无意义请求
        for p in rank_data:
            sid = str(p.get("sid") or "")
            is_multi = bool(split_platform_sid(sid))
            for g in p.get("games", []):
                gid = g.get("gameid")
                if not gid or is_multi:
                    continue
                if not str(gid).isdigit():
                    continue
                resolved = await self.get_chinese_game_name(str(gid), g.get("name"))
                if resolved:
                    g["name"] = resolved

        font_path = self.get_font_path("NotoSansHans-Regular.otf")
        img_bytes = await render_rank_image(
            self.data_dir,
            rank_data,
            period_label,
            font_path=font_path,
            proxy=self.proxy,
            cover_fetcher=cover_fetcher,
            avatar_frame_paths=avatar_frame_paths,
            personal=personal,
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
            tmp.write(img_bytes)
            return tmp.name

    async def _daily_rank_push(self, test_mode=False):
        """推送昨日榜单；默认按目标群独立聚合，显式全局模式才共享总榜。"""
        use_global_rank = getattr(self, "rank_push_all", False)
        scopes = build_rank_push_scopes(
            getattr(self, "rank_push_groups", []),
            use_global_rank=use_global_rank,
        )
        if not scopes:
            logger.warning(
                "[排行榜] 没有目标群可推送"
                "（请先使用 /steam rank_on 或 /steam rank_on all 开启推送）"
            )
            return

        rendered_files = {}
        try:
            for target_group_id, data_group_id in scopes:
                render_key = (
                    ("global", None)
                    if data_group_id is None
                    else ("group", data_group_id)
                )
                if render_key not in rendered_files:
                    rank_data = self._get_rank_data(
                        days=1,
                        group_id=data_group_id,
                        base_day_offset=-1,
                    )
                    if not rank_data:
                        scope_label = (
                            "全部群"
                            if data_group_id is None
                            else f"群 {data_group_id}"
                        )
                        logger.info(
                            f"[排行榜] {scope_label}昨日无游玩记录，跳过推送"
                        )
                        rendered_files[render_key] = None
                    else:
                        try:
                            rendered_files[render_key] = (
                                await self._render_daily_rank_file(rank_data)
                            )
                        except Exception as e:
                            logger.error(
                                f"[排行榜] 渲染群 {data_group_id or '全局'} "
                                f"昨日榜单失败: {e}"
                            )
                            rendered_files[render_key] = None

                tmp_path = rendered_files[render_key]
                if not tmp_path:
                    continue
                try:
                    session = getattr(self, "notify_sessions", {}).get(
                        target_group_id
                    )
                    if not is_sendable_group_session(session):
                        logger.warning(
                            f"[排行榜] 群 {target_group_id} 未找到有效推送会话，跳过"
                        )
                        continue
                    await self.context.send_message(
                        session,
                        MessageChain([
                            Plain("📊 昨日游戏时长排行榜来啦！\n"),
                            Image.fromFileSystem(tmp_path),
                        ]),
                    )
                    logger.info(
                        f"[排行榜] 已推送昨日排行榜到群 {target_group_id}"
                    )
                except Exception as e:
                    logger.error(
                        f"[排行榜] 推送群 {target_group_id} 失败: {e}"
                    )
        except Exception as e:
            logger.error(f"[排行榜] 每日推送异常: {e}")
        finally:
            for tmp_path in {
                path for path in rendered_files.values() if path
            }:
                try:
                    os.unlink(tmp_path)
                except OSError as e:
                    logger.warning(
                        f"[排行榜] 清理临时图片失败 {tmp_path}: {e}"
                    )
    async def _render_and_send_rank(self, event, group_id, days, period_label, is_all=False, base_day_offset=0):
        """生成排行榜图片并发送"""
        try:
            try:
                await self._price_ack(event, f"正在生成{period_label}排行榜…")
            except Exception:
                pass
            rank_data = self._get_rank_data(days=days, group_id=None if is_all else group_id, base_day_offset=base_day_offset)
            if not rank_data:
                yield event.plain_result(f"暂无{period_label}游玩记录，玩家游戏结束后才会有数据。")
                return
            # 补充玩家昵称和头像URL
            sid_set = {p["sid"] for p in rank_data}
            for p in rank_data:
                for s in p.get("sids") or []:
                    sid_set.add(s)
            sid_info = {}
            if sid_set:
                status_map = await self.fetch_player_statuses_batch(list(sid_set))
                for sid, info in status_map.items():
                    sid_info[sid] = {
                        "name": info.get("name") or sid,
                        "avatar_url": info.get("avatarfull") or info.get("avatar")
                    }
                    # 记住 Steam 昵称，避免后续 /rank 在 API 失败时退回数字
                    try:
                        nm = str(info.get("name") or "").strip()
                        if nm and not self._is_steamid_like(nm):
                            key = str(sid)
                            self._steam_persona_names = getattr(self, "_steam_persona_names", None) or {}
                            self._steam_persona_names[key] = nm
                    except Exception:
                        pass
            for p in rank_data:
                info = sid_info.get(p["sid"], {})
                # 头像/昵称（状态 API + 本地缓存）
                if info.get("avatar_url"):
                    p["avatar_url"] = info.get("avatar_url")
                elif not p.get("avatar_url"):
                    try:
                        for s in (p.get("sids") or [p.get("sid")]):
                            if sid_info.get(s, {}).get("avatar_url"):
                                p["avatar_url"] = sid_info[s]["avatar_url"]
                                if not info.get("name"):
                                    info = sid_info[s]
                                break
                    except Exception:
                        pass
                fb = info.get("name")
                if not fb or self._is_steamid_like(fb):
                    fb = self._resolve_player_display_name(p["sid"])
                if not fb or str(fb) == str(p["sid"]) or self._is_steamid_like(fb):
                    sp = split_platform_sid(str(p["sid"]))
                    fb = sp[1] if sp else self._resolve_player_display_name(p["sid"])
                p["name"] = self._resolve_bind_name(p["sid"], fb)
                # 封面：主玩游戏 gameid 必须存在，否则封面空白
                if not p.get("top_game_id") and p.get("games"):
                    top_game = p["games"][0]
                    p["top_game_id"] = top_game.get("gameid") or None
                # 标记主玩游戏ID用于封面获取
                if p["games"]:
                    # 需要gameid来获取封面，从play_records中反查
                    p["top_game_id"] = None
            # 从play_records中反查每个玩家top游戏的gameid
            for p in rank_data:
                if not p["games"]:
                    continue
                top_name = p["games"][0]["name"]
                # 在最近数据中找匹配的gameid
                for di in range(days):
                    dk = self._get_day_key(-di)
                    day_data = self.play_records.get(dk, {})
                    sid_games = day_data.get(p["sid"], {})
                    for gid, ginfo in sid_games.items():
                        if ginfo.get("name") == top_name:
                            p["top_game_id"] = gid
                            break
                    if p.get("top_game_id"):
                        break

            # 封面获取回调
            async def cover_fetcher(gameid):
                path = await self.get_game_cover_url(gameid)
                if path:
                    return path
                try:
                    gid = str(gameid or "")
                    data_dir = getattr(self, "data_dir", "") or ""
                    if gid.isdigit() and data_dir:
                        for sub in ("covers_h", "covers_v", "game_icons", "covers_multi"):
                            for ext in (".jpg", ".png"):
                                alt = os.path.join(data_dir, sub, f"{gid}{ext}")
                                if os.path.exists(alt) and os.path.getsize(alt) > 200:
                                    return alt
                except Exception:
                    pass
                return None

            # 获取头像框路径
            avatar_frame_paths = {}
            from ..presentation.renderers.game_start import get_avatar_frame_url, get_avatar_frame_path
            for p in rank_data:
                sid = p.get("sid", "")
                if sid:
                    fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                    if not fp:
                        frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                        if frame_url:
                            fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                    if fp:
                        avatar_frame_paths[sid] = fp

            # 排行榜游戏名统一转中文名来源（覆盖插件重启/缓存污染等写入的英文名）
            for p in rank_data:
                for g in p.get("games", []):
                    gid = g.get("gameid")
                    if not gid:
                        continue
                    resolved = await self.get_chinese_game_name(str(gid), g.get("name"))
                    if resolved:
                        g["name"] = resolved

            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_rank_image(
                self.data_dir, rank_data, period_label,
                font_path=font_path, proxy=self.proxy,
                cover_fetcher=cover_fetcher,
                avatar_frame_paths=avatar_frame_paths
            )
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        except Exception as e:
            logger.error(f"[排行榜] 渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"排行榜生成失败: {e}")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam rank")
    async def steam_rank(self, event: AstrMessageEvent, period: str = ""):
        '''时长排行：/steam rank | /steam rank 昨天 | /steam rank week|month|天数'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        group_id = event.get_group_id() or "default"
        period = (period or "").strip().lower()
        if period in ("昨天", "昨日", "yesterday", "yd", "-1"):
            days, label, offset = 1, "昨日", -1
        elif period == "week":
            days, label, offset = 7, "最近7天", 0
        elif period == "month":
            days, label, offset = 30, "最近30天", 0
        elif period.isdigit():
            days = int(period)
            if days <= 0:
                days = 1
            label, offset = f"最近{days}天", 0
        else:
            days, label, offset = 1, "今日", 0
        async for result in self._render_and_send_rank(
            event, group_id, days, label, is_all=False, base_day_offset=offset
        ):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam allrank")
    async def steam_allrank(self, event: AstrMessageEvent, period: str = ""):
        '''查看所有群玩家游戏时长排行榜（默认今日，可选 昨天/week/month）'''
        period = (period or "").strip().lower()
        if period in ("昨天", "昨日", "yesterday", "yd", "-1"):
            days, label, offset = 1, "昨日", -1
        elif period == "week":
            days, label, offset = 7, "最近7天", 0
        elif period == "month":
            days, label, offset = 30, "最近30天", 0
        elif period.isdigit():
            days = int(period)
            if days <= 0:
                days = 1
            label, offset = f"最近{days}天", 0
        else:
            days, label, offset = 1, "今日", 0
        async for result in self._render_and_send_rank(
            event, None, days, label, is_all=True, base_day_offset=offset
        ):
            yield result

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam rank_on")
    async def steam_rank_on(self, event: AstrMessageEvent, param: str = ""):
        '''每日排行榜推送管理；参数: all=全局排行, list=查看状态, test=即刻推送, del [群号]=删除推送'''
        param = param.strip().lower()
        if param == "list":
            is_all = getattr(self, 'rank_push_all', False)
            groups = list(self.rank_push_groups)
            if groups:
                mode = '全局' if is_all else '分群'
                yield event.plain_result(f"当前排行榜推送模式：{mode}排行，推送群：{', '.join(groups)}")
            else:
                yield event.plain_result("当前未开启任何排行榜推送。使用 /steam rank_on 或 /steam rank_on all 开启。")
            return
        if param == "test":
            yield event.plain_result("正在生成昨日排行榜，稍等...")
            await self._daily_rank_push(test_mode=True)
            return
        if param.startswith("del"):
            parts = param.split()
            if len(parts) >= 2:
                target = parts[1]
            else:
                target = event.get_group_id() or "default"
            if target in self.rank_push_groups:
                self.rank_push_groups.remove(target)
                self._save_rank_push_groups()
                yield event.plain_result(f"已关闭群 {target} 的每日排行榜推送。")
            else:
                yield event.plain_result(f"群 {target} 未在推送列表中。")
            return
        if param == "all":
            self.rank_push_all = True
            group_id = event.get_group_id() or "default"
            if not is_valid_group_id(group_id):
                yield event.plain_result("请在群聊中开启排行榜推送。")
                return
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
            self._save_rank_push_groups()
            yield event.plain_result("已开启每日排行榜自动推送（全局排行）")
        else:
            self.rank_push_all = False
            group_id = event.get_group_id() or "default"
            if not is_valid_group_id(group_id):
                yield event.plain_result("请在群聊中开启排行榜推送。")
                return
            if group_id not in self.rank_push_groups:
                self.rank_push_groups.append(group_id)
                self._save_rank_push_groups()
            yield event.plain_result(f"已开启本群每日排行榜自动推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单同步")
    async def steam_qq_menu_sync(self, event: AstrMessageEvent):
        """创建或更新 QQ 官方机器人指令面板。"""
        yield event.plain_result(await self.qq_menu_sync(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单状态")
    async def steam_qq_menu_status(self, event: AstrMessageEvent):
        """查询 QQ 官方机器人指令面板状态。"""
        yield event.plain_result(await self.qq_menu_status(event))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam qq菜单删除")
    async def steam_qq_menu_delete(self, event: AstrMessageEvent):
        """删除本插件记录的 QQ 官方机器人指令面板。"""
        yield event.plain_result(await self.qq_menu_delete(event))

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam help")
    async def steam_help(self, event: AstrMessageEvent):
        '''显示所有指令帮助'''
        # 优先发送帮助图
        help_img = os.path.join(self.data_dir, "help_menu.png")
        # 插件自带 assets
        try:
            from ..shared.paths import IMAGES_DIR
            bundled = str(IMAGES_DIR / "help_menu.png")
            if os.path.exists(bundled):
                help_img = bundled
        except Exception:
            pass
        if os.path.exists(help_img):
            try:
                yield event.image_result(help_img)
                return
            except Exception as e:
                logger.warning(f"帮助图发送失败，回退文字: {e}")
        help_text = (
            "Steam状态监控插件指令：\n"
            "/steam on - 启动监控\n"
            "/steam off - 停止监控\n"
            "/price [游戏名或Steam链接] - 查询价格、Steam史低与地区对比\n"
            "/px [游戏名] - 价格快捷版（直接返回第一条匹配）\n"
            "/steam list - 列出所有玩家状态\n"
            "/steam config - 查看当前配置\n"
            "/steam set [参数] [值] - 设置配置参数\n"
            "/steam addid [SteamID] - 添加SteamID\n"
            "/steam addid psn:<在线ID> / xbox:<Gamertag|XUID> - 添加多平台玩家\n"
            "/steam delid [SteamID] - 删除SteamID\n"
            "/steam xbox <Gamertag|XUID> - 调试Xbox玩家状态（管理员）\n"
            "/steam sim_xbox <Gamertag> <游戏名> - 模拟Xbox开局推送（管理员）\n"
            "/steam test_xbox_ach <Gamertag> [titleId] - 测试Xbox成就推送（管理员）\n"
            "/steam sim_psn <在线ID> <游戏名> - 模拟PSN开局推送（管理员）\n"
            "/steam push_group [SteamID] - 添加id到联动推送的副群\n"
            "/steam delpush_group [SteamID] [群号可选] - 删除id联动推送的副群，可指定群号\n"
            "/steam openbox [SteamID] - 查看指定SteamID的全部信息\n"
            "/steam rank - 查看本群今日游戏时长排行榜\n"
            "/steam rank 天数 - 查看本群指定天数排行榜（如 7, 30）\n"
            "/steam allrank - 查看所有群今日排行榜\n"
            "/steam allrank 天数 - 查看所有群指定天数排行榜\n"
            "/steam alllist [img|text] - 查看所有群聊玩家状态（默认图片，text 纯文本）\n"
            "/steam rank_on [all|list|test|del] - 管理每日排行榜推送（可配置时间）\n"
            "/steam rank_on list - 查看推送状态\n"
            "/steam rank_on del [群号] - 删除指定群推送（默认本群）\n"
            "/steam fonts - 查看字体包状态（检测CJK字体是否就绪）\n"
            "/steam fonts download - 立即下载字体包\n"
            "/steam fonts clean - 清理已下载字体缓存\n"
            "/game activity - 查看最近购游戏日志\n"
            "/game activity @某人 - 查看某人购游戏记录\n"
            "/game wish_sale on|off|status|update|check|test|cache|list - 愿望单（手动拉列表+缓存查折扣+小时限额）\n"
            "/game ach @某人 - 全库扫全成就（进度约每1/3提示）\n"
            "/game ach stop - 手动停止进行中的全成就扫描\n"
            "/steam rs - 清除状态并初始化\n"
            "/steamwho @用户 / 在干嘛 @用户 - 即时查询绑定玩家的Steam状态\n"
            "/game bind [@某人|QQ号] - 查询绑定账号与备注\n"
            "/game bind all|list - 本群全部绑定一览\n"
            "/game bind 7656xxx / psn:xxx / xbox:xxx - 反查该账号绑的QQ\n"
            "/game remark <ID> 新备注 - 修改绑定备注\n"
            "/game remark @某人 新备注 - 改该QQ的备注\n"
            "/game doc / /game 手册 - 发送群友指令PDF手册\n"
            "/mybind - 查自己绑定\n"
            "/steam help - 显示本帮助\n"
        )
        yield event.plain_result(help_text)

    def _sid_explicit_remark(self, sid) -> str:
        """单独备注键 __remark:{sid}；无则空串。"""
        sid = str(sid or "")
        ent = (getattr(self, "_bind_data", {}) or {}).get(f"__remark:{sid}")
        if isinstance(ent, dict):
            nick = ent.get("nickname")
            if nick and str(nick) != "*":
                return str(nick)
        return ""

    def _format_qq_bind_report(self, qq_clean: str) -> str:
        """生成「某 QQ 绑定了哪些账号 + 备注」报告。"""
        qq_clean = str(qq_clean or "").strip()
        bind = getattr(self, "_bind_data", {}) or {}
        info = bind.get(qq_clean)
        lines = [f"🔗 QQ {qq_clean} 的绑定信息"]
        if not isinstance(info, dict):
            # 也检查是否有仅备注、但 sid 曾绑过该 QQ 的情况
            orphan = []
            for sid_key, ent in bind.items():
                if not str(sid_key).startswith("__remark:"):
                    continue
                sids = self._bind_info_sids(ent)
                # remark 键本身不挂 QQ，无法反查
            lines.append("未绑定任何账号。")
            lines.append("绑定示例：/game steam add 7656xxx @某人 备注名")
            lines.append("或：/game ps add psn:在线ID @某人 / /game xbox add xbox:Gamertag @某人")
            return "\n".join(lines)

        sids = self._bind_info_sids(info)
        qq_nick = info.get("nickname")
        if qq_nick in (None, "", "*"):
            qq_nick_disp = "（未设置）"
        else:
            qq_nick_disp = str(qq_nick)
        lines.append(f"绑定备注：{qq_nick_disp}")
        if not sids:
            lines.append("绑定数据里没有账号 ID（异常），请重新绑定。")
            return "\n".join(lines)

        plat_label = {
            "psn": "PSN",
            "xbox": "Xbox",
            "nso": "Nintendo",
        }

        def _fmt_sid(sid: str) -> str:
            sp = split_platform_sid(str(sid))
            if sp:
                return f"[{plat_label.get(sp[0], sp[0].upper())}] {sp[1]}"
            return f"[Steam] {sid}"

        lines.append(f"共绑定 {len(sids)} 个账号：")
        group_ids = set()
        for ids in (self.group_steam_ids or {}).values():
            group_ids.update(str(x) for x in ids)

        for i, sid in enumerate(sids, 1):
            sid = str(sid)
            per_sid = self._sid_explicit_remark(sid)
            # 显示名：单独备注 > QQ备注 > 状态缓存昵称 > ID
            state_name = ""
            for states in (getattr(self, "group_last_states", {}) or {}).values():
                st = (states or {}).get(sid) or {}
                if st.get("name"):
                    state_name = str(st.get("name"))
                    break
            remark = per_sid or (str(qq_nick) if qq_nick not in (None, "", "*") else "")
            parts = [f"{i}. {_fmt_sid(sid)}"]
            if remark:
                parts.append(f"备注：{remark}")
            else:
                parts.append("备注：无")
            if state_name and not split_platform_sid(sid):
                parts.append(f"Steam名：{state_name}")
            elif state_name:
                parts.append(f"平台名：{state_name}")
            monitored = "是" if sid in group_ids else "否"
            parts.append(f"本插件监控：{monitored}")
            lines.append(" · ".join(parts))

        # 仅有备注、未挂在该 QQ 下的账号（不展示为绑定，仅提示）
        remarks_only = []
        for key, ent in bind.items():
            if not str(key).startswith("__remark:"):
                continue
            rid = str(key.split(":", 1)[-1])
            if rid in {str(s) for s in sids}:
                continue
            nick = (ent or {}).get("nickname") if isinstance(ent, dict) else None
            if nick and nick != "*":
                remarks_only.append(f"{_fmt_sid(rid)}（{nick}）")
        if remarks_only:
            lines.append("其他仅备注（未绑到本QQ）：" + "、".join(remarks_only[:8]))
        lines.append("解绑/改备注：/game steam add <ID> @某人 新备注 或管理端删除")
        return "\n".join(lines)

    @staticmethod
    def _steam_friend_code(steamid: str) -> str:
        try:
            return str(int(str(steamid)) - 76561197960265728)
        except Exception:
            return ""

    def _all_steam_sids_for_qq(self, qq) -> list:
        out = []
        info = (getattr(self, "_bind_data", {}) or {}).get(str(qq)) or {}
        for s in self._bind_info_sids(info):
            s = str(s)
            if s.isdigit() and len(s) == 17 and s not in out:
                out.append(s)
        return out

    def _primary_steam_sid_for_qq(self, qq) -> str:
        """QQ 绑定多个 SteamID 时，优先选「在监控列表且有显示名」的那个。"""
        sids = self._all_steam_sids_for_qq(qq)
        if not sids:
            return ""
        if len(sids) == 1:
            return sids[0]
        monitored = set()
        for ids in (getattr(self, "group_steam_ids", {}) or {}).values():
            monitored.update(str(x) for x in ids)

        def score(sid: str):
            sc = 0
            if sid in monitored:
                sc += 100
            try:
                name = self._state_name_for_sid(sid)
                if name and name != sid:
                    sc += 20
            except Exception:
                pass
            for states in (getattr(self, "group_last_states", {}) or {}).values():
                st = (states or {}).get(sid) or {}
                if st.get("gameid") or st.get("name"):
                    sc += 5
                    break
            return sc

        sids.sort(key=lambda x: (-score(x), x))
        return sids[0]

    @staticmethod
    def _fmt_bind_sid(sid: str) -> str:
        plat_label = {"psn": "PSN", "xbox": "Xbox", "nso": "Nintendo"}
        sp = split_platform_sid(str(sid))
        if sp:
            return f"[{plat_label.get(sp[0], sp[0].upper())}] {sp[1]}"
        return f"[Steam] {sid}"

    def _state_name_for_sid(self, sid: str) -> str:
        sid = str(sid)
        for states in (getattr(self, "group_last_states", {}) or {}).values():
            st = (states or {}).get(sid) or {}
            if st.get("name"):
                return str(st.get("name"))
        return ""

    def _qq_entries_for_sid(self, sid: str):
        """反查：sid 对应的所有 QQ 绑定条目。

        返回 [{"qq","nickname","remark"}]
        """
        sid = str(sid or "").strip()
        out = []
        bind = getattr(self, "_bind_data", {}) or {}
        for qq, info in bind.items():
            if str(qq).startswith("__remark:"):
                continue
            if not isinstance(info, dict):
                continue
            if sid not in self._bind_info_sids(info):
                continue
            nick = info.get("nickname")
            remark = self._sid_explicit_remark(sid) or (str(nick) if nick not in (None, "", "*") else "")
            out.append({
                "qq": str(qq),
                "nickname": "" if nick in (None, "", "*") else str(nick),
                "remark": remark,
            })
        # 仅有 __remark、无 QQ 的情况
        if not out:
            only = self._sid_explicit_remark(sid)
            if only:
                out.append({"qq": "", "nickname": only, "remark": only})
        return out

    def _format_sid_reverse_report(self, sid: str) -> str:
        sid = str(sid).strip()
        lines = [f"🔍 账号反查：{self._fmt_bind_sid(sid)}"]
        state_name = self._state_name_for_sid(sid)
        if state_name:
            lines.append(f"显示名：{state_name}")
        bind_hits = self._qq_entries_for_sid(sid)
        if bind_hits:
            lines.append(f"绑定到 {len(bind_hits)} 个 QQ：")
            for i, h in enumerate(bind_hits, 1):
                if h["qq"]:
                    nick = h["nickname"] or "（无备注）"
                    lines.append(f"{i}. QQ {h['qq']} · 备注：{nick}")
                else:
                    lines.append(f"{i}. 仅备注（无QQ）· {h['remark']}")
        else:
            lines.append("未找到绑定到任何 QQ 的记录。")
            lines.append(f"单独备注：{self._sid_explicit_remark(sid) or '无'}")
        # 是否在监控
        in_groups = []
        for gid, ids in (self.group_steam_ids or {}).items():
            if sid in {str(x) for x in ids}:
                in_groups.append(str(gid))
        if in_groups:
            lines.append(f"监控群：{', '.join(in_groups[:8])}" + ("…" if len(in_groups) > 8 else ""))
        else:
            lines.append("监控：未加入本插件监控列表")
        return "\n".join(lines)

    def _format_group_bind_report(self, group_id: str) -> str:
        """本群绑定一览：精简排版，按平台分组。"""
        group_id = str(group_id or "").strip()
        bind = getattr(self, "_bind_data", {}) or {}
        g_sids = [str(s) for s in (self.group_steam_ids.get(group_id) or [])]

        sid_to_qqs = {}
        for qq, info in bind.items():
            if str(qq).startswith("__remark:"):
                continue
            if not isinstance(info, dict):
                continue
            for sid in self._bind_info_sids(info):
                sid_to_qqs.setdefault(str(sid), []).append(str(qq))

        def _qq_nick(qq):
            info = bind.get(str(qq)) or {}
            nick = info.get("nickname") if isinstance(info, dict) else None
            return "" if nick in (None, "", "*") else str(nick)

        def _plat_of(sid: str):
            sp = split_platform_sid(sid)
            if sp:
                return sp[0].upper(), sp[1]
            return "Steam", sid

        def _show_name(sid: str, raw: str) -> str:
            name = self._state_name_for_sid(sid)
            if name and name != raw:
                return name
            if _plat_of(sid)[0] == "Steam":
                # Steam 无昵称时显示短 ID
                return raw if len(raw) <= 8 else f"{raw[:4]}…{raw[-4:]}"
            return raw

        bound = []  # (plat, display, sid, qqs)
        unbound = []  # (plat, display)
        for sid in g_sids:
            plat, raw = _plat_of(sid)
            display = _show_name(sid, raw)
            qqs = sid_to_qqs.get(sid) or []
            if qqs:
                bound.append((plat, display, sid, qqs))
            else:
                unbound.append((plat, display))

        plat_order = {"Steam": 0, "PSN": 1, "Xbox": 2, "Nintendo": 3}
        bound.sort(key=lambda x: (plat_order.get(x[0], 9), x[1]))
        unbound.sort(key=lambda x: (plat_order.get(x[0], 9), x[1]))

        lines = [
            f"📋 群绑定一览（{group_id}）",
            f"监控 {len(g_sids)} · 已绑定 {len(bound)} · 未绑定 {len(unbound)}",
            "",
            "【已绑定】",
        ]
        if not bound:
            lines.append("（无）")
        else:
            # 同一 QQ 多账号时，备注只在有值时显示一次
            seen_qq_remark = {}
            for plat, display, sid, qqs in bound:
                qparts = []
                for qq in qqs:
                    nick = _qq_nick(qq)
                    if nick and seen_qq_remark.get(qq) != nick:
                        qparts.append(f"{qq}（{nick}）")
                        seen_qq_remark[qq] = nick
                    else:
                        qparts.append(qq)
                # 平台宽 6 字，便于对齐
                lines.append(f"{plat:<6} {display} → {'、'.join(qparts)}")

        # 未绑定：按平台压缩成一行，逗号分隔
        by_plat = {}
        for plat, display in unbound:
            by_plat.setdefault(plat, []).append(display)
        lines.append("")
        lines.append("【未绑定】")
        if not unbound:
            lines.append("（无）")
        else:
            for plat in sorted(by_plat.keys(), key=lambda p: plat_order.get(p, 9)):
                names = by_plat[plat]
                shown = "、".join(names[:12])
                more = f" 等{len(names)}人" if len(names) > 12 else f"（{len(names)}）"
                lines.append(f"{plat}：{shown}{more}")

        lines.append("")
        lines.append("反查：/game bind 7656xxx 或 psn:/xbox:账号")
        lines.append("个人：/game bind @某人  ·  自己：/mybind")
        return "\n".join(lines)

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game bind")
    async def game_bind_query(self, event: AstrMessageEvent, target: str = ""):
        '''绑定查询：/game bind [@某人|QQ|all|SteamID/psn:/xbox:]'''
        raw_target = str(target or "").strip()
        raw_msg = ""
        for getter_name in ("get_message_str", "get_message"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    raw_msg = str(getter() or "")
                    if raw_msg:
                        break
                except Exception:
                    pass
        # 去掉指令本身，得到参数
        arg = raw_target
        if not arg and raw_msg:
            arg = re.sub(r"^[/.。／]*\s*(?:game\s+)?bind\s*", "", raw_msg, flags=re.I).strip()
        arg_l = arg.lower()

        # 1) 群全部
        if arg_l in ("all", "list", "群", "全部", "本群", "group", "*"):
            group_id = str(event.get_group_id() or "").strip()
            if not group_id:
                yield event.plain_result("请在群聊中使用 /game bind all")
                return
            yield event.plain_result(self._format_group_bind_report(group_id))
            return

        # 2) 平台账号 / SteamID 反查
        sid_guess = None
        if arg:
            if split_platform_sid(arg):
                sid_guess = arg
            else:
                m_sid = re.search(r"\b(7656\d{13})\b", arg)
                if m_sid:
                    sid_guess = m_sid.group(1)
                elif re.fullmatch(r"\d{17}", arg.strip()):
                    sid_guess = arg.strip()
        if sid_guess:
            yield event.plain_result(self._format_sid_reverse_report(sid_guess))
            return

        # 3) QQ / @某人 / 默认自己
        qq_clean = None
        for text in (arg, raw_msg):
            if not text:
                continue
            m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', text)
            if m:
                qq_clean = m.group(1) or m.group(2) or m.group(3) or m.group(4)
                break
        if not qq_clean and arg:
            m2 = re.search(r"\b([1-9]\d{4,11})\b", arg)
            if m2:
                qq_clean = m2.group(1)
        if not qq_clean:
            qq_clean = str(event.get_sender_id() or "").strip()
        if not qq_clean or not qq_clean.isdigit():
            yield event.plain_result(
                "用法：\n"
                "/game bind\n"
                "/game bind @某人 或 QQ号\n"
                "/game bind all（本群全部）\n"
                "/game bind 7656xxx / psn:xxx / xbox:xxx（反查QQ）"
            )
            return
        yield event.plain_result(self._format_qq_bind_report(qq_clean))

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game remark")
    async def game_remark(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        '''修改绑定备注：
        /game remark <SteamID|psn:|xbox:> 新备注
        /game remark @某人 新备注
        /game remark @某人 <ID> 新备注
        清空备注：新备注写 清空 / clear / -
        '''
        raw_msg = ""
        for getter_name in ("get_message_str", "get_message"):
            getter = getattr(event, getter_name, None)
            if callable(getter):
                try:
                    raw_msg = str(getter() or "")
                    if raw_msg:
                        break
                except Exception:
                    pass
        # 拼出完整参数串（兼容框架拆参）
        parts = [str(x).strip() for x in (arg1, arg2, arg3) if str(x or "").strip()]
        raw = " ".join(parts).strip()
        if not raw and raw_msg:
            raw = re.sub(r"^[/.。／]*\s*(?:game\s+)?remark\s*", "", raw_msg, flags=re.I).strip()
        if not raw:
            yield event.plain_result(
                "用法：\n"
                "/game remark 7656xxx 新备注\n"
                "/game remark psn:在线ID 新备注\n"
                "/game remark @某人 新备注\n"
                "清空：/game remark 7656xxx 清空"
            )
            return

        # 解析 @ / QQ / 平台ID / SteamID / 备注
        qq_clean = None
        sid = None
        m_at = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', raw)
        if m_at:
            qq_clean = m_at.group(1) or m_at.group(2) or m_at.group(3) or m_at.group(4)
            raw_wo = (raw[:m_at.start()] + " " + raw[m_at.end():]).strip()
        else:
            raw_wo = raw

        tokens = raw_wo.split()
        # 识别平台 ID / SteamID
        remain = []
        for t in tokens:
            if sid:
                remain.append(t)
                continue
            tt = t.strip()
            if split_platform_sid(tt):
                sid = tt
                continue
            m_st = re.fullmatch(r"(7656\d{13})", tt)
            if m_st:
                sid = m_st.group(1)
                continue
            if re.fullmatch(r"\d{17}", tt):
                sid = tt
                continue
            # Steam 好友码/链接交给 resolve（仅当后面还有备注词时）
            remain.append(t)

        # 备注 = 剩余词（去掉可能误抓的纯 QQ 号）
        remark_tokens = []
        for t in remain:
            if qq_clean is None and re.fullmatch(r"[1-9]\d{4,11}", t):
                qq_clean = t
                continue
            remark_tokens.append(t)
        new_remark = " ".join(remark_tokens).strip()
        clear_words = {"清空", "clear", "none", "删除", "-", "无"}
        do_clear = new_remark.lower() in clear_words or new_remark in clear_words
        if do_clear:
            new_remark = ""

        # 尝试把非平台串解析成 SteamID（好友码/链接）
        if not sid and remark_tokens:
            maybe = remark_tokens[0]
            if not split_platform_sid(maybe) and (maybe.isdigit() or "steamcommunity" in maybe or "profiles" in maybe):
                try:
                    resolved = await self.resolve_steam_input(maybe)
                    if resolved and str(resolved).isdigit() and len(str(resolved)) == 17:
                        sid = str(resolved)
                        remark_tokens = remark_tokens[1:]
                        new_remark = " ".join(remark_tokens).strip()
                        do_clear = new_remark.lower() in clear_words or new_remark in clear_words
                        if do_clear:
                            new_remark = ""
                except Exception:
                    pass

        if not sid and not qq_clean:
            yield event.plain_result(
                "请指定要改备注的对象：\n"
                "/game remark 7656xxx 新备注\n"
                "/game remark @某人 新备注"
            )
            return
        if not do_clear and not new_remark:
            yield event.plain_result("请写上新备注，或用「清空」删除备注。")
            return

        if not hasattr(self, "_bind_data") or self._bind_data is None:
            self._bind_data = {}
        bind = self._bind_data
        changed = []

        # 情况 A：只改 QQ 级备注
        if qq_clean and not sid:
            info = bind.get(str(qq_clean))
            if not isinstance(info, dict) or not self._bind_info_sids(info):
                yield event.plain_result(f"QQ {qq_clean} 未绑定任何账号，无法改备注。")
                return
            old = info.get("nickname")
            info["nickname"] = new_remark if new_remark else "*"
            # 同步已绑定各 sid 的单独备注，保持显示一致
            for s in self._bind_info_sids(info):
                if new_remark:
                    bind[f"__remark:{s}"] = {"sids": [str(s)], "nickname": new_remark}
                else:
                    bind.pop(f"__remark:{s}", None)
            self._save_bind_data()
            old_disp = "（空）" if old in (None, "", "*") else str(old)
            new_disp = new_remark or "（空）"
            yield event.plain_result(
                f"已更新 QQ {qq_clean} 的备注：{old_disp} → {new_disp}\n"
                f"关联账号：{'、'.join(self._bind_info_sids(info))}"
            )
            return

        # 情况 B：指定 sid（可选 @QQ）
        if sid:
            # Steam 未解析完整时提示
            if not split_platform_sid(sid) and not (str(sid).isdigit() and len(str(sid)) == 17):
                yield event.plain_result("账号 ID 无效，支持 17位SteamID64 / psn:xxx / xbox:xxx")
                return
            sid = str(sid)
            # 单号备注
            if new_remark:
                bind[f"__remark:{sid}"] = {"sids": [sid], "nickname": new_remark}
            else:
                bind.pop(f"__remark:{sid}", None)
            # 同步挂在这个 sid 上的 QQ 备注（若该 QQ 只有这一个 sid，或用户指定了 QQ）
            hit_qqs = []
            for qq, info in list(bind.items()):
                if str(qq).startswith("__remark:"):
                    continue
                if not isinstance(info, dict):
                    continue
                if sid not in self._bind_info_sids(info):
                    continue
                if qq_clean and str(qq) != str(qq_clean):
                    continue
                hit_qqs.append(str(qq))
                if new_remark:
                    info["nickname"] = new_remark
                elif qq_clean:
                    # 明确针对该 QQ 清空
                    info["nickname"] = "*"
                # 只清单号备注时，不改 QQ 级昵称（可能是多账号共用）
            if qq_clean and not hit_qqs:
                # 指定了 QQ 但未绑这个 sid：补一条绑定备注（不自动 add 进监控）
                info = bind.get(str(qq_clean)) or {}
                sids = list(self._bind_info_sids(info))
                if sid not in sids:
                    sids.append(sid)
                bind[str(qq_clean)] = {
                    "sids": sids,
                    "nickname": new_remark if new_remark else (info.get("nickname") if info.get("nickname") not in (None, "") else "*"),
                }
                hit_qqs.append(str(qq_clean))
            self._save_bind_data()
            old_note = self._state_name_for_sid(sid) or sid
            new_disp = new_remark or "（空）"
            msg = [f"已更新备注：{self._fmt_bind_sid(sid)}"]
            if old_note and old_note != sid:
                msg.append(f"原显示名：{old_note}")
            msg.append(f"新备注：{new_disp}")
            if hit_qqs:
                msg.append(f"同步 QQ：{'、'.join(hit_qqs)}")
            else:
                msg.append("未绑定 QQ（仅写入单号备注）")
            yield event.plain_result("\n".join(msg))
            return

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam remark")
    async def steam_remark_alias(self, event: AstrMessageEvent, arg1: str = "", arg2: str = "", arg3: str = ""):
        '''改备注别名：/steam remark …'''
        async for r in self.game_remark(event, arg1, arg2, arg3):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("mybind")
    async def mybind_query(self, event: AstrMessageEvent):
        '''查询自己绑定的账号与备注：/mybind'''
        qq_clean = str(event.get_sender_id() or "").strip()
        if not qq_clean:
            yield event.plain_result("无法获取你的 QQ 号，请在群里使用 /mybind")
            return
        yield event.plain_result(self._format_qq_bind_report(qq_clean))

    def _member_manual_pdf_path(self):
        """群友指令手册 PDF：优先插件 assets，其次数据目录。"""
        candidates = []
        try:
            from ..shared.paths import ASSETS_DIR
            candidates.append(str(ASSETS_DIR / "docs" / "game_member_manual.pdf"))
        except Exception:
            pass
        try:
            plug_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            candidates.append(os.path.join(plug_root, "assets", "docs", "game_member_manual.pdf"))
        except Exception:
            pass
        candidates.append(os.path.join(getattr(self, "data_dir", "") or "", "game_member_manual.pdf"))
        for p in candidates:
            if p and os.path.isfile(p) and os.path.getsize(p) > 1000:
                return p
        return None

    def _member_manual_page_paths(self):
        """手册 PDF 预渲染页图（合并转发兜底）。"""
        dirs = []
        try:
            from ..shared.paths import ASSETS_DIR
            dirs.append(str(ASSETS_DIR / "docs" / "manual_pages"))
        except Exception:
            pass
        try:
            plug_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
            dirs.append(os.path.join(plug_root, "assets", "docs", "manual_pages"))
        except Exception:
            pass
        dirs.append(os.path.join(getattr(self, "data_dir", "") or "", "manual_pages"))
        pages = []
        for d in dirs:
            if not d or not os.path.isdir(d):
                continue
            found = sorted(
                os.path.join(d, fn) for fn in os.listdir(d)
                if fn.lower().endswith(".png") and os.path.isfile(os.path.join(d, fn))
            )
            if found:
                return found
        return pages

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game doc")
    async def game_doc(self, event: AstrMessageEvent):
        '''发送群友可用指令手册（优先 PDF 文件，失败则合并转发页图）'''
        pdf_path = self._member_manual_pdf_path()
        bot = getattr(event, "bot", None)
        group_id = str(event.get_group_id() or "").strip()
        user_id = str(event.get_sender_id() or "").strip()
        display_name = "游戏监控_群友指令手册.pdf"
        ascii_name = "game_member_manual.pdf"

        # NapCat realpath 很可能在【宿主机】上执行，容器内 /AstrBot/... 会 ENOENT。
        # 因此同时尝试：容器路径 + 宿主机路径（即使 astrbot 进程里 os.path 为 False）。
        path_candidates = []
        if pdf_path:
            path_candidates.append(pdf_path)
        path_candidates.extend([
            "/AstrBot/data/steam_status_monitor/game_member_manual.pdf",
            "/AstrBot/data/plugins/steam_status_monitor_V3/assets/docs/game_member_manual.pdf",
            "/app/napcat/config/game_member_manual.pdf",
            "/app/.config/QQ/game_member_manual.pdf",
            # 宿主机视角（关键）
            "/root/astrbot/data/steam_status_monitor/game_member_manual.pdf",
            "/root/astrbot/data/plugins/steam_status_monitor_V3/assets/docs/game_member_manual.pdf",
            "/root/astrbot/napcat/config/game_member_manual.pdf",
            "/root/astrbot/ntqq/game_member_manual.pdf",
        ])
        seen = set()
        uniq = []
        for p in path_candidates:
            if p and p not in seen:
                seen.add(p)
                uniq.append(p)

        async def _try_raw_file() -> bool:
            """直接 call_action send_msg + file 段，绕过 AstrBot File 组件。"""
            if not bot or not hasattr(bot, "call_action"):
                return False
            is_group = bool(group_id and group_id not in ("", "default") and group_id.isdigit())
            is_user = bool(user_id and user_id.isdigit())
            if not is_group and not is_user:
                return False
            for p in uniq:
                for file_val in (p, f"file://{p}"):
                    msg = [{
                        "type": "file",
                        "data": {"file": file_val, "name": ascii_name},
                    }]
                    try:
                        if is_group:
                            await bot.call_action("send_msg", group_id=int(group_id), message=msg)
                        else:
                            await bot.call_action("send_msg", user_id=int(user_id), message=msg)
                        logger.info(f"[game_doc] send_msg file OK val={file_val}")
                        return True
                    except Exception as ex:
                        logger.warning(f"[game_doc] send_msg file 失败 val={file_val}: {ex}")
            return False

        try:
            if await _try_raw_file():
                yield event.plain_result(f"📄 已发送手册文件（{ascii_name}）\n也可发 /game help 看速查图")
                return
        except Exception as e:
            logger.warning(f"[game_doc] raw file 异常: {e}")

        # 文件发不出去 → 手册页图合并转发（与愿望单同链路，QQ 可显示）
        pages = self._member_manual_page_paths()
        if pages:
            try:
                from astrbot.api.message_components import Node, Nodes, Image as ImgComp, Plain
                bot_uin = "0"
                bot_name = "游戏监控"
                try:
                    info = await bot.get_login_info() if bot else None
                    if isinstance(info, dict):
                        bot_name = str(info.get("nickname") or bot_name)
                        bot_uin = str(info.get("user_id") or bot_uin)
                except Exception:
                    pass
                header = (
                    "📄 群友指令手册（合并转发页图）\n"
                    "QQ 文件消息受限，以下为手册全文页图，点开查看。\n"
                    f"共 {len(pages)} 页 · 也可发 /game help 看速查图"
                )
                nodes = [Node(uin=bot_uin or "0", name=bot_name, content=[Plain(header)])]
                for i, pp in enumerate(pages, 1):
                    if not os.path.isfile(pp):
                        continue
                    nodes.append(Node(
                        uin=bot_uin or "0",
                        name=bot_name,
                        content=[
                            Plain(f"手册 · 第 {i}/{len(pages)} 页"),
                            ImgComp.fromFileSystem(pp),
                        ],
                    ))
                if len(nodes) > 1:
                    yield event.chain_result([Nodes(nodes)])
                    return
            except Exception as e:
                logger.warning(f"[game_doc] 页图合并转发失败: {e}")
            # 逐页图片兜底
            try:
                from astrbot.api.message_components import Image as ImgComp, Plain
                yield event.plain_result("📄 群友指令手册（逐页图片）")
                for i, pp in enumerate(pages, 1):
                    if os.path.isfile(pp):
                        yield event.chain_result([
                            Plain(f"第 {i}/{len(pages)} 页"),
                            ImgComp.fromFileSystem(pp),
                        ])
                return
            except Exception as e:
                logger.warning(f"[game_doc] 逐页图片失败: {e}")

        # 兜底文字
        try:
            from astrbot.api.message_components import File, Plain
            if pdf_path and os.path.isfile(pdf_path):
                yield event.chain_result([
                    Plain("📄 群友指令手册"),
                    File(name=display_name, file=pdf_path),
                ])
                return
        except Exception as e:
            logger.warning(f"[game_doc] File 组件失败: {e}")

        yield event.plain_result(
            "手册发送失败（协议端文件接口不可用且无页图）。\n"
            "请管理员确认 assets/docs/manual_pages/ 已部署。\n"
            "群友常用：/price /game wish @某人 /game bind /rank /mybind /game help"
        )

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game 手册")
    async def game_doc_alias(self, event: AstrMessageEvent):
        '''发送群友指令手册（别名）'''
        async for r in self.game_doc(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam doc")
    async def steam_doc_alias(self, event: AstrMessageEvent):
        '''发送群友指令 PDF 手册'''
        async for r in self.game_doc(event):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam bind")
    async def steam_bind_alias(self, event: AstrMessageEvent, target: str = ""):
        '''查询绑定：/steam bind [@某人|all|SteamID]'''
        async for r in self.game_bind_query(event, target):
            yield r

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam openbox")
    async def steam_openbox(self, event: AstrMessageEvent, steamid: str):
        '''查询指定SteamID的全部API返回信息'''
        if not self.API_KEY:
            yield event.plain_result("未配置 Steam API Key，请先在插件配置中填写 steam_api_key。")
            return
        sid = await self.resolve_steam_input(steamid)
        if not sid or not sid.isdigit() or len(sid) != 17:
            yield event.plain_result("无法解析为有效SteamID，支持格式：17位SteamID64 / 个人资料链接 / 自定义ID链接 / 8位好友码")
            return
        async for result in handle_openbox(self, event, sid):
            yield result

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steamwho")
    async def steam_who(self, event: AstrMessageEvent, qq: str):
        '''查询指定QQ绑定的Steam玩家状态（ /steamwho @用户 或 /在干嘛 @用户 ）'''
        blk = self._steam_guard_block_msg()
        if blk:
            yield event.plain_result(blk)
            return
        m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)|[^\s()]{1,64}\((\d{5,})\)', qq.strip()); qq_clean = (m.group(1) or m.group(2) or m.group(3) or m.group(4) or m.group(5)) if m else qq.strip().lstrip('@')
        # 参数里拿不到 QQ 时，从事件消息链取 @ 目标
        if not str(qq_clean or "").strip().isdigit():
            _at_ev2 = self._extract_at_qq_from_event(event)
            if _at_ev2:
                qq_clean = _at_ev2
        if not str(qq_clean or "").strip().isdigit():
            _qq_nick2 = await self._resolve_qq_by_nickname(event, qq)
            if _qq_nick2:
                qq_clean = _qq_nick2
        info = getattr(self, "_bind_data", {}).get(qq_clean)
        if not info:
            yield event.plain_result(f"QQ {qq_clean} 未绑定任何玩家，请先使用 /game steam add … @{qq_clean}")
            return
        sids = self._bind_info_sids(info)
        if not sids:
            yield event.plain_result(f"QQ {qq_clean} 的绑定数据异常")
            return
        status_map = await self.fetch_player_statuses_batch(sids)
        now = int(time.time())
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        user_list = []
        for sid in sids:
            status = status_map.get(sid) or await self.fetch_player_status(sid)
            if not status:
                user_list.append({'sid': sid, 'name': self._resolve_bind_name(sid, sid), 'status': 'offline', 'avatar_url': '', 'game': '', 'gameid': '', 'play_str': '状态获取失败', 'lastlogoff': None})
                continue
            name = self._resolve_bind_name(sid, status.get('name') or sid)
            gameid = status.get('gameid')
            game = status.get('gameextrainfo')
            personastate = status.get('personastate', 0)
            avatar_url = status.get('avatarfull') or status.get('avatar') or ''
            lastlogoff = status.get('lastlogoff')
            is_multi = bool(split_platform_sid(str(sid)))
            zh_game_name = (game or '') if is_multi or not gameid else (await self.get_chinese_game_name(gameid, game) or game or '')
            if gameid:
                start_time = self.session_service.started_at(group_id, sid, gameid)
                play_seconds = now - start_time if start_time else 0
                play_minutes = play_seconds / 60
                play_str = f"{play_minutes/60:.1f}小时" if play_minutes >= 60 else f"{play_minutes:.1f}分钟"
                user_list.append({'sid': sid, 'name': name, 'status': 'playing', 'avatar_url': avatar_url, 'game': zh_game_name, 'gameid': gameid, 'play_str': play_str, 'lastlogoff': lastlogoff})
            elif personastate and int(personastate) > 0:
                _persona_status = {0: 'offline', 1: 'online', 2: 'busy', 3: 'away', 4: 'snooze'}
                p_status = _persona_status.get(int(personastate), 'online')
                user_list.append({'sid': sid, 'name': name, 'status': p_status, 'avatar_url': avatar_url, 'game': '', 'gameid': '', 'play_str': '', 'lastlogoff': lastlogoff})
            else:
                hours_ago = (now - int(lastlogoff)) / 3600 if lastlogoff else 0
                play_str = f"上次在线 {hours_ago:.1f}小时前" if lastlogoff else ''
                user_list.append({'sid': sid, 'name': name, 'status': 'offline', 'avatar_url': avatar_url, 'game': '', 'gameid': '', 'play_str': play_str, 'lastlogoff': lastlogoff})
        from ..presentation.renderers.render_assets import gather_avatar_frames, gather_steam_covers
        avatar_frame_paths = await gather_avatar_frames(self.data_dir, sids, proxy=self.proxy)
        from ..presentation.renderers.steam_list import render_steam_list_image
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        steam_style = self.config.get('enable_steam_style', False)
        covers = {}
        if not steam_style:
            covers = await gather_steam_covers(self, user_list, proxy=self.proxy)
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy, avatar_frame_paths=avatar_frame_paths, covers=covers, steam_style=steam_style)
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("game time")
    async def game_time(self, event: AstrMessageEvent, target: str = "", days: str = ""):
        '''查询单人游玩时长（与 /rank 同源，多平台已合并）：/game time @某人 [天数]'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        raw = [x for x in (target, days) if x and str(x).strip()]
        qq_clean = None
        n_days = 1
        for part in raw:
            part = str(part).strip()
            m = re.search(r'\[CQ:at,qq=(\d+)\]|\[At:(\d+)\]|@.+?\((\d+)\)|@(\d+)', part)
            if m and not qq_clean:
                qq_clean = m.group(1) or m.group(2) or m.group(3) or m.group(4)
                continue
            if part.isdigit():
                n_days = max(1, min(90, int(part)))
                continue
            if not qq_clean and part.lstrip('@').isdigit() and len(part.lstrip('@')) >= 5:
                qq_clean = part.lstrip('@')
        if not qq_clean:
            yield event.plain_result("请 @ 要查询的成员，例如：/game time @某人 或 /game time @某人 7")
            return
        period_label = "今日" if n_days == 1 else f"最近{n_days}天"
        bind_sids = set(self._bind_sids_for_qq(qq_clean))
        if not bind_sids:
            yield event.plain_result(f"QQ {qq_clean} 未绑定任何玩家，请先 /game steam add … @{qq_clean}")
            return
        rank_data = self._get_rank_data(days=n_days, group_id=group_id)
        if not rank_data:
            yield event.plain_result(f"群内暂无{period_label}游玩记录。")
            return
        target_row = None
        for p in rank_data:
            sids = set(str(s) for s in (p.get("sids") or [p.get("sid")]) if s)
            if sids & bind_sids:
                target_row = p
                break
        if not target_row:
            yield event.plain_result(f"{period_label}没有 QQ {qq_clean} 的游玩时长记录。")
            return
        sid_set = set(str(s) for s in (target_row.get("sids") or [target_row.get("sid")]) if s)
        status_map = await self.fetch_player_statuses_batch(list(sid_set)) if sid_set else {}
        fb = target_row.get("name")
        for s in sid_set:
            info = status_map.get(s) or {}
            if not fb:
                sp = split_platform_sid(str(s))
                fb = info.get("name") or (sp[1] if sp else str(s)[-8:])
            if not target_row.get("avatar_url"):
                target_row["avatar_url"] = info.get("avatarfull") or info.get("avatar")
        target_row["name"] = self._resolve_bind_name(target_row.get("sid"), fb or str(target_row.get("sid")))
        info0 = status_map.get(str(target_row.get("sid"))) or {}
        if info0:
            target_row["avatar_url"] = info0.get("avatarfull") or info0.get("avatar") or target_row.get("avatar_url")
        if target_row.get("games"):
            target_row["top_game_id"] = target_row["games"][0].get("gameid")
        try:
            tmp_path = await self._render_daily_rank_file([target_row], period_label=period_label, personal=True)
        except Exception as e:
            logger.error(f"[game time] 渲染失败: {e}")
            yield event.plain_result("时长卡片渲染失败")
            return
        if not tmp_path:
            yield event.plain_result("时长卡片渲染失败")
            return
        yield event.image_result(tmp_path)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("在干嘛")
    async def steam_zai_gan_ma(self, event: AstrMessageEvent, qq: str):
        '''/在干嘛 @用户 —— steamwho 的别名'''
        async for r in self.steam_who(event, qq):
            yield r

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam off")
    async def steam_off(self, event: AstrMessageEvent):
        '''彻底停止本群Steam状态监控轮询，释放轮询资源'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_monitor_enabled[group_id] = False
        if group_id in self.running_groups:
            self.running_groups.remove(group_id)
        self._save_group_switches()
        # 清除该群的轮询时间表，停止轮询（/steam on 后会重新初始化）
        self.next_poll_time.pop(group_id, None)
        # 停用后不再推送本群缓冲通知；会话仍保留，由 tick_due 到期结算时长
        self._pending_end_notifications.pop(group_id, None)
        # 取消该群所有成就轮询任务，释放资源
        keys_to_cancel = [k for k in list(self.achievement_poll_tasks.keys()) if k[0] == group_id]
        for key in keys_to_cancel:
            task = self.achievement_poll_tasks.pop(key, None)
            if task:
                task.cancel()
        yield event.plain_result(f"已为本群彻底关闭Steam监控，轮询已停止。使用 /steam on 可重新启动。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_on")
    async def steam_achievement_on(self, event: AstrMessageEvent):
        """开启本群Steam成就推送"""
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_achievement_enabled[group_id] = True
        self._save_group_switches()
        yield event.plain_result(f"已为本群开启Steam成就推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam achievement_off")
    async def steam_achievement_off(self, event: AstrMessageEvent):
        """关闭本群Steam成就推送"""
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        self.group_achievement_enabled[group_id] = False
        self._save_group_switches()
        yield event.plain_result(f"已为本群关闭Steam成就推送。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_perfect")
    async def steam_test_perfect(self, event: AstrMessageEvent, steamid: str, appid: str):
        '''测试全成就检测与推送：/steam test_perfect <SteamID|psn:x|xbox:x> <appid>'''
        from ..infrastructure.clients.multi import split_platform_sid
        gid = str(event.get_group_id()) if hasattr(event, "get_group_id") else "default"
        sp = split_platform_sid(str(steamid))
        if sp:
            sid = f"{sp[0]}:{sp[1]}"
        else:
            sid = await self.resolve_steam_input(steamid)
            if not sid:
                yield event.plain_result(f"无法解析 ID：{steamid}（支持 SteamID64 / 资料链接 / 好友码 / psn:x / xbox:x）")
                return
        aid = str(appid).strip()
        if not aid.isdigit():
            yield event.plain_result("appid 必须是数字，例如：/steam test_perfect 76561199415792116 730")
            return
        try:
            status = await self.fetch_player_status(sid)
            player_name = (status or {}).get("name") or self._resolve_bind_name(sid, sid)
        except Exception:
            player_name = self._resolve_bind_name(sid, sid)
        try:
            game_name = await self.get_chinese_game_name(int(aid), None) or f"appid {aid}"
        except Exception:
            game_name = f"appid {aid}"
        try:
            if sp and sp[0] == "xbox":
                unlocked = await self.fetch_xbox_title_achievements(sp[1], aid)
            else:
                unlocked = await self.achievement_monitor.get_player_achievements(
                    self.API_KEY, gid, sid, int(aid))
            total = await self._total_achievement_count(gid, sid, aid, game_name)
        except Exception as e:
            yield event.plain_result(f"获取成就数据失败：{format_exception(e)}")
            return
        n = len(unlocked or [])
        lines = [
            "🧪 全成就推送 · 测试",
            f"玩家：{player_name}（{sid}）",
            f"游戏：{game_name}（{aid}）",
            f"已解锁：{n}    总成就：{total}",
            f"判定：{'✅ 已达成全成就' if (total and n >= total) else '❌ 尚未达成全成就'}",
        ]
        if total and n >= total:
            ok = await self.notify_perfect_achievement(
                gid, sid, player_name, aid, game_name, total, n, is_new=True, force=True)
            lines.append("推送：已发送 ✅（测试模式，不写入去重记录）" if ok else "推送：失败或无有效推送会话 ❌")
        else:
            lines.append("提示：换一个已全成就的账号+游戏再试")
        yield event.plain_result("\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_achievement_render")
    async def steam_test_achievement_render(self, event: AstrMessageEvent, steamid: str, gameid: int, count: int = 3):
        '''测试成就消息渲染效果（steam test_achievement_render [steamid] [gameid] [数量]）'''
        player_name = steamid
        game_name = await self.get_chinese_game_name(gameid)
        group_id = self.GROUP_ID or 'default'
        achievements = await self.achievement_monitor.get_player_achievements(self.API_KEY, group_id, steamid, gameid)
        if not achievements:
            yield event.plain_result("未获取到任何成就，可能为隐私或无成就。")
            return
        details = await self.achievement_monitor.get_achievement_details(group_id, gameid, lang="schinese", api_key=self.API_KEY, steamid=steamid)
        import random
        count = max(1, min(count, len(achievements)))
        unlocked = set(random.sample(list(achievements), count))
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        # 直接测试 Pillow 渲染
        try:
            img_bytes = await self.achievement_monitor.render_achievement_image(details, unlocked, player_name=player_name, font_path=font_path)
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        except Exception as e:
            import traceback
            logger.error(f"成就图片渲染失败: {e}\n{traceback.format_exc()}")
            # 回退文本
            msg = self.achievement_monitor.render_achievement_message(details, unlocked, player_name=player_name)
            yield event.plain_result(msg)

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam sim_psn")
    async def steam_sim_psn(self, event: AstrMessageEvent, online_id: str, game_name: str, game_id: str = ""):
        '''模拟 PSN 玩家开局推送（测试真实推送链路）：steam sim_psn <在线ID> <游戏名> [gameid]'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sid = f"psn:{online_id}"
        real = await self.fetch_player_status(sid) or {}
        status = dict(real)
        gid = game_id or real.get("gameid") or "CUSA00000"
        status.update({
            "name": real.get("name") or online_id,
            "gameid": gid,
            "gameextrainfo": game_name,
            "personastate": 1,
        })
        logger.info(f"[sim_psn] 模拟开局推送: {sid} 《{game_name}》 -> 群 {group_id}")
        await self.session_service.handle(
            group_id, sid, gid, int(time.time()),
            player_name=real.get("name") or online_id,
            current_game_name=game_name,
            status=status, skip_push=False,
        )
        await self._flush_pending_end_notifications()
        yield event.plain_result(f"✅ 已模拟 {online_id} 开始玩《{game_name}》并走真实推送链路")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam xbox")
    async def steam_xbox_debug(self, event: AstrMessageEvent, gamertag_or_xuid: str):
        '''调试 Xbox 单个玩家状态：steam xbox <Gamertag或XUID>'''
        summary = await self.fetch_xbox_debug_status(gamertag_or_xuid)
        tokens = self._xbox_tokens_path()
        yield event.plain_result(f"[Xbox调试]\ntokens: {tokens}\n{summary}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam sim_xbox")
    async def steam_sim_xbox(self, event: AstrMessageEvent, gamertag: str, game_name: str, game_id: str = ""):
        '''模拟 Xbox 玩家开局推送：steam sim_xbox <Gamertag> <游戏名> [gameid]'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sid = f"xbox:{gamertag}"
        real = await self.fetch_player_status(sid) or {}
        status = dict(real)
        gid = game_id or real.get("gameid") or "XBOX0000"
        status.update({
            "name": real.get("name") or gamertag,
            "gameid": gid,
            "gameextrainfo": game_name,
            "personastate": 1,
            "platform": "xbox",
        })
        logger.info(f"[sim_xbox] 模拟开局推送: {sid} 《{game_name}》 -> 群 {group_id}")
        await self.session_service.handle(
            group_id, sid, gid, int(time.time()),
            player_name=real.get("name") or gamertag,
            current_game_name=game_name,
            status=status, skip_push=False,
        )
        await self._flush_pending_end_notifications()
        yield event.plain_result(f"✅ 已模拟 {gamertag} 开始玩《{game_name}》并走真实推送链路")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_xbox_ach")
    async def steam_test_xbox_ach(self, event: AstrMessageEvent, gamertag: str, title_id: str = ""):
        '''测试 Xbox 成就获取与推送：steam test_xbox_ach <Gamertag> [titleId]
        不带 titleId 时取最近有解锁的 title 并推送其中 1 条'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sid = f"xbox:{gamertag}"
        # 1) 拉最近成就/title 列表
        recent = await self.fetch_xbox_recent_unlocked_titles(gamertag, limit=8)
        if title_id:
            candidates = [str(title_id)]
            match = next((t for t in recent if str(t.get("titleId")) == str(title_id)), None)
            game_name = (match or {}).get("name") or title_id
        else:
            if not recent:
                yield event.plain_result(
                    "未拉到最近 title。请指定 titleId：\n/steam test_xbox_ach <Gamertag> <titleId>"
                )
                return
            candidates = [str(t["titleId"]) for t in recent if t.get("titleId")]
            game_name = None
        # 2) 找到有已解锁成就的 title
        unlocked = None
        used_tid = None
        used_name = None
        for tid in candidates:
            u = await self.fetch_xbox_title_achievements(gamertag, tid)
            if u:
                unlocked = u
                used_tid = tid
                used_name = next((t.get("name") for t in recent if str(t.get("titleId")) == tid), None) or game_name or tid
                break
        if unlocked is None:
            yield event.plain_result(
                f"成就拉取失败或均无已解锁成就。\nrecent={[(t.get('titleId'), t.get('name'), t.get('earned')) for t in recent[:5]]}"
            )
            return
        details = await self.fetch_xbox_achievement_details(gamertag, used_tid, game_name=used_name)
        summary = f"titleId={used_tid} 游戏={used_name} 已解锁={len(unlocked)}/{len(details or {})}"
        pick = next(iter(unlocked))
        yield event.plain_result(f"拉取成功：{summary}\n将推送成就：{pick}")
        await self.notify_new_achievements(group_id, sid, gamertag, used_tid, used_name, {pick})
        yield event.plain_result("✅ Xbox 成就测试推送已发出")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_start_render")
    async def test_game_start_render(self, event: AstrMessageEvent, steamid: str, gameid: str):
        '''测试开始游戏图片渲染效果（steam test_game_start_render [steamid] [gameid]）
        支持 PSN 等平台的字符串 gameid（如 CUSA07325），Steam appid 仍为数字'''
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            from ..infrastructure.clients.multi import split_platform_sid
            is_multi = bool(split_platform_sid(str(steamid)))
            if is_multi:
                # 多平台：直接使用 PSN 状态里的游戏名与封面
                zh_game_name = (status.get("gameextrainfo") if status else None) or str(gameid)
                en_game_name = zh_game_name
                img_bytes = await render_game_start(
                    self.data_dir, steamid, player_name, avatar_url, str(gameid), zh_game_name,
                    api_key=None, superpower=self.get_today_superpower(steamid),
                    sgdb_api_key=None, font_path=self.get_font_path('NotoSansHans-Regular.otf'),
                    sgdb_game_name=en_game_name, online_count=None, appid=str(gameid),
                    proxy=self.proxy, version=self._plugin_version,
                    cover_url=(status.get("cover_url") if status else None),
                )
            else:
                zh_game_name, en_game_name = await self.get_game_names(gameid)
                logger.info(f"[测试开始游戏渲染] steamid={steamid} gameid={gameid} player_name={player_name} avatar_url={avatar_url} zh_game_name={zh_game_name} en_game_name={en_game_name}")
                superpower = self.get_today_superpower(steamid)
                print(f"[superpower] test_game_start_render superpower={superpower}")
                font_path = self.get_font_path('NotoSansHans-Regular.otf')
                online_count = await self.get_game_online_count(gameid)
                img_bytes = await render_game_start(
                    self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name, api_key=self.API_KEY, superpower=superpower, sgdb_api_key=self.SGDB_API_KEY, font_path=font_path, sgdb_game_name=en_game_name, online_count=online_count, appid=gameid
                    , proxy=self.proxy, version=self._plugin_version, sgdb_api_base=self.SGDB_API_BASE, steam_store_base=self.STEAM_STORE_BASE)
            logger.info(f"[测试开始游戏渲染] render_game_start 返回类型: {type(img_bytes)} 长度: {len(img_bytes) if img_bytes else 'None'}")
            if img_bytes:
                import tempfile
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                    tmp.write(img_bytes)
                    tmp_path = tmp.name
                img = PILImage.open(tmp_path).convert("RGB")
                cropped_img = self.crop_image_auto(img, bg_color=(51,81,66), threshold=15)
                with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp2:
                    cropped_img.save(tmp2, format="PNG")
                    tmp_path = tmp2.name
                logger.info(f"[测试开始游戏渲染] 已保存裁剪图到 {tmp_path}")
                yield event.image_result(tmp_path)
            else:
                yield event.plain_result("渲染失败，未获取到图片数据。")
        except Exception as e:
            logger.error(f"测试开始游戏图片渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"渲染异常: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam test_game_end_render")
    async def steam_test_game_end_render(self, event: AstrMessageEvent, steamid: str, gameid: int, duration_min: float = 120, end_time: str = None, tip_text: str = None):
        '''测试游戏结束图片渲染（steam test_game_end_render [steamid] [gameid] [时长分钟] [结束时间 可选] [提示 可选]）'''
        try:
            status = await self.fetch_player_status(steamid)
            player_name = self._resolve_bind_name(steamid, status.get("name") if status else steamid)
            avatar_url = status.get("avatarfull") or status.get("avatar") or "" if status else ""
            zh_game_name, en_game_name = await self.get_game_names(gameid)
            logger.info(f"[get_game_names] zh_game_name={zh_game_name}, en_game_name={en_game_name}")  # 新增英文名输出
            from datetime import datetime
            if end_time:
                end_time_str = end_time
            else:
                end_time_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            duration_h = float(duration_min) / 60 if duration_min else 0
            if not tip_text:
                if duration_min < 5:
                    tip_text = "风扇都没转热，主人就结束了？"
                elif duration_min < 10:
                    tip_text = "杂鱼杂鱼~主人你就这水平？"
                elif duration_min < 30:
                    tip_text = "热身一下就结束了？"
                elif duration_min < 60:
                    tip_text = "歇会儿再来，别太累了喵！"
                elif duration_min < 120:
                    tip_text = "沉浸在游戏世界，时间过得飞快喵！"
                elif duration_min < 300:
                    tip_text = "肝到手软了喵！主人不如陪陪咱~"
                elif duration_min < 600:
                    tip_text = "你吃饭了吗？还是说你已经忘了吃饭这件事？"
                elif duration_min < 1200:
                    tip_text = "家里电费都要被你玩光了喵！"
                elif duration_min < 1800:
                    tip_text = "咱都要给你颁发‘不眠猫’勋章了！"
                elif duration_min < 2400:
                    tip_text = "主人你还活着喵？你是不是忘了关电脑呀~"
                else:
                    tip_text = "你已经和椅子合为一体，成为传说中的‘椅子精’了喵！"
            font_path = self.get_font_path('NotoSansHans-Regular.otf')
            img_bytes = await render_game_end(
                self.data_dir, steamid, player_name, avatar_url, gameid, zh_game_name,
                end_time_str, tip_text, duration_h, sgdb_api_key=self.SGDB_API_KEY, font_path=font_path, sgdb_game_name=en_game_name, appid=gameid
            , proxy=self.proxy, api_key=self.API_KEY)
            msg = f"👋 {player_name} 不玩 {zh_game_name} 了\n游玩时间 {duration_h:.1f}小时"
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.plain_result(msg)
            yield event.image_result(tmp_path)
        except Exception as e:
            import traceback
            logger.error(f"测试游戏结束图片渲染失败: {e}\n{traceback.format_exc()}")
            yield event.plain_result(f"渲染异常: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam fonts")
    async def steam_fonts(self, event: AstrMessageEvent, param: str = ""):
        '''字体包管理；参数: download=立即下载, clean=清理缓存'''
        service = getattr(self, "font_pack", None)
        if service is None:
            yield event.plain_result("字体服务未初始化。")
            return
        action = (param or "").strip().lower()
        if action in ("", "status"):
            yield event.plain_result(service.format_status_text())
            return
        if action == "clean":
            yield event.plain_result(service.clean())
            return
        if action in ("download", "update"):
            last_sent = 0.0

            async def progress_cb(payload):
                nonlocal last_sent
                now = time.time()
                if not payload.get("force") and now - last_sent < 2:
                    return
                last_sent = now
                text = (
                    "正在下载字体包...\n"
                    f"{payload['bar']}\n"
                    f"预计剩余：{payload['eta_text']}"
                )
                try:
                    await event.send(event.plain_result(text))
                except Exception:
                    logger.info("[Font] %s", text.replace("\n", " "))

            yield event.plain_result("开始下载字体包，完成后会更新状态。")
            result = await service.download_now(progress_cb=progress_cb)
            yield event.plain_result(result)
            return
        yield event.plain_result("用法：/steam fonts、/steam fonts download、/steam fonts clean")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam清除缓存")
    async def steam_clear_cache(self, event: AstrMessageEvent):
        '''清除所有头像、封面图等图片缓存（慎用）'''
        try:
            cache_dirs = [
                os.path.join(self.data_dir, "avatars"),
                os.path.join(self.data_dir, "covers"),
                os.path.join(self.data_dir, "covers_v"),
            ]
            cleared = []
            for d in cache_dirs:
                if os.path.exists(d):
                    shutil.rmtree(d)
                    cleared.append(d)
            msg = "已清除以下缓存目录：\n" + "\n".join(cleared) if cleared else "未找到任何缓存目录，无需清理。"
            yield event.plain_result(msg)
        except Exception as e:
            yield event.plain_result(f"清除缓存失败: {e}")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_allids")
    async def steam_clear_allids(self, event: AstrMessageEvent):
        '''删除所有群聊的所有已监控SteamID，并清空相关状态数据'''
        for task in self.achievement_poll_tasks.values():
            task.cancel()
        self.achievement_poll_tasks.clear()
        self.achievement_snapshots.clear()
        self.achievement_fail_count.clear()
        self.group_steam_ids.clear()
        self.push_groups.clear()
        self.running_groups.clear()
        self.group_monitor_enabled.clear()
        self.group_achievement_enabled.clear()
        self._save_group_switches()
        self.next_poll_time.clear()
        self.group_last_states.clear()
        self.group_last_quit_times.clear()
        self.group_pending_logs.clear()
        self.playing_sessions.clear()
        getattr(self, "_session_meta", {}).clear()
        self.group_recent_games.clear()
        self._pending_end_notifications.clear()
        self.notify_sessions.clear()
        self._save_group_steam_ids()
        self._save_push_groups()
        self._save_notify_session()
        self._save_persistent_data(force=True)
        self.config['group_steam_ids'] = self.group_steam_ids
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result("已删除所有群聊的所有监控玩家，相关状态数据已清空。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam clear_groupids")
    async def steam_clear_groupids(self, event: AstrMessageEvent, group_id: str):
        '''删除指定群聊的所有已监控SteamID，并清空相关状态数据'''
        has_primary = group_id in self.group_steam_ids
        routed_sids = [
            sid for sid, targets in self.push_groups.items()
            if group_id in {str(target) for target in targets}
        ]
        if not has_primary and not routed_sids:
            yield event.plain_result(f"群聊 {group_id} 未绑定任何监控玩家，无需清理。")
            return

        for sid in list(self.group_steam_ids.get(group_id, [])):
            self.push_groups.pop(sid, None)
        for sid in routed_sids:
            targets = [target for target in self.push_groups.get(sid, []) if str(target) != group_id]
            if targets:
                self.push_groups[sid] = targets
            else:
                self.push_groups.pop(sid, None)
        self.group_steam_ids.pop(group_id, None)
        self.group_last_states.pop(group_id, None)
        self.group_last_quit_times.pop(group_id, None)
        self.group_pending_logs.pop(group_id, None)
        self.session_service.discard_group(group_id)
        self.group_recent_games.pop(group_id, None)
        self.next_poll_time.pop(group_id, None)
        self.running_groups.discard(group_id)
        self.group_monitor_enabled.pop(group_id, None)
        self.group_achievement_enabled.pop(group_id, None)
        self.notify_sessions.pop(group_id, None)
        self._save_group_steam_ids()
        self._save_push_groups()
        self._save_notify_session()
        self._save_persistent_data(force=True)
        if hasattr(self.config, "save_config"):
            self.config.save_config()
        yield event.plain_result(f"已删除群聊 {group_id} 的所有监控玩家和分发路由，相关状态数据已清空。")

    def _should_skip_game(self, gameid):
        """根据黑白名单配置判断是否应跳过该游戏的监控/播报"""
        if not gameid:
            return False
        mode = self.config.get('game_filter_mode', '全部游戏')
        if mode == '全部游戏':
            return False
        ids_str = self.config.get('game_filter_ids', '')
        if not ids_str or not ids_str.strip():
            return False
        try:
            filter_ids = [x.strip() for x in ids_str.split(',') if x.strip()]
        except Exception:
            return False
        if mode == '白名单':
            return str(gameid) not in filter_ids
        elif mode == '黑名单':
            return str(gameid) in filter_ids
        return False

    def _get_day_key(self, offset_days=0):
        """基于凌晨4:00边界的日期键
        offset_days=0: 当前所处"天"的日期键
        offset_days=-1: 前一天的日期键
        """
        now = datetime.now()
        if now.hour < 4:
            now = now - timedelta(days=1)
        now = now + timedelta(days=offset_days)
        return now.strftime("%Y-%m-%d")

    def _get_rank_data(self, days=1, group_id=None, base_day_offset=0):
        """聚合游玩时长数据，返回已排序的排行榜列表
        Args:
            days: 1=今日, 7=最近7天, 30=最近30天
            group_id: 指定群则只统计该群的SteamID，None则统计全部
        Returns:
            [{sid, name, total_minutes, games: [{name, minutes}]}] 按总时长降序
        """
        try:
            today_str = self._get_day_key(base_day_offset)
            base_date = datetime.strptime(today_str, "%Y-%m-%d")
            date_keys = []
            for i in range(days):
                d = base_date - timedelta(days=i)
                date_keys.append(d.strftime("%Y-%m-%d"))
            # 确定要统计的 SteamID 集合
            if group_id:
                target_sids = set(self.group_steam_ids.get(group_id, []))
            else:
                target_sids = set()
                for gids in self.group_steam_ids.values():
                    target_sids.update(gids)
            if not target_sids:
                return []
            # 聚合
            merged = {}  # {sid: {gameid: {name, minutes}}}
            for date_key in date_keys:
                day_data = self.play_records.get(date_key, {})
                for sid, games in day_data.items():
                    if sid not in target_sids:
                        continue
                    if sid not in merged:
                        merged[sid] = {}
                    for gid, info in games.items():
                        # 防御性清洗：name 可能被缓存污染为 tuple/list
                        raw_name = info.get("name", "未知游戏")
                        if isinstance(raw_name, (tuple, list)):
                            raw_name = raw_name[0] if raw_name else "未知游戏"
                        raw_name = str(raw_name) if raw_name else "未知游戏"
                        if gid not in merged[sid]:
                            sp = split_platform_sid(str(sid))
                            merged[sid][gid] = {
                                "name": raw_name,
                                "minutes": 0,
                                "platform": (sp[0].upper() if sp else "STEAM"),
                            }
                        merged[sid][gid]["minutes"] += info.get("minutes", 0)
                        merged[sid][gid]["name"] = info.get("name", merged[sid][gid]["name"])
            # 构建排行榜列表
            rank_list = []
            for sid, games in merged.items():
                total = sum(g["minutes"] for g in games.values())
                if total <= 0:
                    continue
                game_list = sorted(
                    [{
                        "name": g["name"],
                        "minutes": g["minutes"],
                        "gameid": gid,
                        "platform": g.get("platform") or "STEAM",
                    } for gid, g in games.items()],
                    key=lambda x: x["minutes"],
                    reverse=True
                )
                rank_list.append({
                    "sid": sid,
                    "name": self._resolve_player_display_name(sid),
                    "total_minutes": total,
                    "games": game_list
                })
            rank_list.sort(key=lambda x: x["total_minutes"], reverse=True)
            rank_list = self._merge_cross_platform_rank(rank_list)
            return rank_list
        except Exception as e:
            logger.error(f"[排行榜] 聚合数据异常: {e}")
            return []

    def _merge_cross_platform_rank(self, rank_list):
        """同一人多平台（绑同 QQ / 同备注名）合并为一行，标注平台。"""
        if not rank_list:
            return rank_list
        bind = getattr(self, "_bind_data", {}) or {}

        def identity(sid: str):
            sid = str(sid)
            qq = self._qq_of_bound_sid(sid)
            if qq:
                nick = str((bind.get(qq) or {}).get("nickname") or "").strip()
                return ("qq", qq, nick if nick and nick != "*" else "")
            return ("sid", sid, "")

        def display_key(name: str):
            return re.sub(r"\s+", "", str(name or "")).casefold()

        buckets = {}
        order = []
        # 预扫描：Steam 显示名 -> identity key，用于把 xbox:EniVILL 并到 EniVILL
        name_to_key = {}
        for p in rank_list:
            sid = str(p.get("sid") or "")
            if not split_platform_sid(sid):
                name_to_key[display_key(p.get("name"))] = identity(sid)
                name_to_key[display_key(sid)] = identity(sid)

        for p in rank_list:
            sid = p.get("sid")
            key = identity(sid)
            sp = split_platform_sid(str(sid))
            if sp and sp[0] in ("xbox", "psn"):
                # xbox:EniVILL / psn:EniVILL → 若已有同名 Steam 行则并入
                nk = display_key(sp[1])
                if nk in name_to_key:
                    key = name_to_key[nk]
            elif not sp:
                name_to_key.setdefault(display_key(p.get("name")), key)
            if key not in buckets:
                buckets[key] = {
                    "sids": [],
                    "platforms": set(),
                    "games": {},  # name -> {minutes, gameid}
                    "total_minutes": 0,
                    "name": p.get("name"),
                }
                order.append(key)
            b = buckets[key]
            b["sids"].append(sid)
            sp = split_platform_sid(str(sid))
            b["platforms"].add(sp[0].upper() if sp else "STEAM")
            b["total_minutes"] += p.get("total_minutes") or 0
            for g in p.get("games") or []:
                n = g.get("name") or "未知游戏"
                plat = g.get("platform") or (
                    (split_platform_sid(str(sid))[0].upper() if split_platform_sid(str(sid)) else "STEAM")
                )
                cur = b["games"].get(n) or {"minutes": 0, "gameid": g.get("gameid") or "", "platform": plat}
                cur["minutes"] += g.get("minutes") or 0
                if not cur.get("gameid") and g.get("gameid"):
                    cur["gameid"] = g.get("gameid")
                if not cur.get("platform"):
                    cur["platform"] = plat
                b["games"][n] = cur

        merged = []
        for key in order:
            b = buckets[key]
            games = sorted(
                ({"name": n, "minutes": m["minutes"], "gameid": m.get("gameid") or "",
                  "platform": m.get("platform") or "STEAM"}
                 for n, m in b["games"].items()),
                key=lambda x: x["minutes"],
                reverse=True,
            )
            # 显示名：优先备注/QQ 绑定名，否则第一个 sid 的展示名
            display = b["name"]
            for sid in b["sids"]:
                display = self._resolve_bind_name(sid, display)
                if display and not self._is_steamid_like(display) and str(display) != str(sid):
                    break
            merged.append({
                "sid": b["sids"][0],
                "sids": b["sids"],
                "platforms": " · ".join(sorted(b["platforms"])),
                "name": display,
                "total_minutes": b["total_minutes"],
                "games": games,
            })
        merged.sort(key=lambda x: x["total_minutes"], reverse=True)
        return merged

    def _record_playtime(self, sid, gameid, game_name, duration_min):
        """记录游玩时长到 play_records，带5分钟去重（防止多群重复记录）"""
        try:
            if duration_min <= 0 or not gameid:
                return
            # 防御性清洗：确保 game_name 是字符串（可能被缓存污染为 tuple/list）
            if isinstance(game_name, (tuple, list)):
                game_name = game_name[0] if game_name else "未知游戏"
            game_name = str(game_name) if game_name else "未知游戏"
            cache_key = (str(sid), str(gameid))
            now = time.time()
            last_ts = self._recorded_quit_cache.get(cache_key, 0)
            if now - last_ts < 300:
                logger.debug(f"[排行榜] 去重跳过: {sid} {game_name} (上次记录{int(now-last_ts)}秒前)")
                return
            self._recorded_quit_cache[cache_key] = now
            today_key = self._get_day_key(0)
            if today_key not in self.play_records:
                self.play_records[today_key] = {}
            if str(sid) not in self.play_records[today_key]:
                self.play_records[today_key][str(sid)] = {}
            gid = str(gameid)
            sp = split_platform_sid(str(sid))
            plat = sp[0].upper() if sp else "STEAM"
            if gid not in self.play_records[today_key][str(sid)]:
                self.play_records[today_key][str(sid)][gid] = {
                    "name": game_name, "minutes": 0, "platform": plat,
                }
            self.play_records[today_key][str(sid)][gid]["minutes"] += int(duration_min)
            self.play_records[today_key][str(sid)][gid]["name"] = game_name
            self._data_dirty = True
            logger.info(f"[排行榜] 记录游玩时长: {sid} {game_name} +{int(duration_min)}分钟")
            # 清理过期的去重缓存（超过10分钟）
            expired = [k for k, v in self._recorded_quit_cache.items() if now - v > 600]
            for k in expired:
                self._recorded_quit_cache.pop(k, None)
        except Exception as e:
            logger.error(f"[排行榜] 记录游玩时长异常: {e}")

    async def get_game_online_count(self, gameid):
        '''通过 Steam Web API 获取当前游戏在线人数'''
        if not gameid:
            return None
        url = f"{self.STEAM_API_BASE}/ISteamUserStats/GetNumberOfCurrentPlayers/v1/?appid={gameid}"
        try:
            async with shared_httpx_client(proxy=self.proxy, timeout=10, follow_redirects=False) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get('response', {}).get('player_count')
        except Exception as e:
            logger.warning(f"获取在线人数失败: {format_exception(e)} (gameid={gameid})")
        return None

    @filter.permission_type(filter.PermissionType.MEMBER)
    @filter.command("steam alllist")
    async def steam_alllist(self, event: AstrMessageEvent, mode: str = "img"):
        '''本群玩家在玩/在线总览（默认图片，text 输出文本）'''
        _persona_status = {0: 'offline', 1: 'online', 2: 'busy', 3: 'away', 4: 'snooze'}
        from ..presentation.renderers.steam_list import render_steam_list_image
        user_list = []
        now = int(time.time())
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        steam_ids = list(self.group_steam_ids.get(group_id) or [])
        if not steam_ids:
            yield event.plain_result("本群未设置监控玩家列表，请先添加。")
            return
        next_poll = self.next_poll_time.get(group_id, {})
        status_map = await self.fetch_player_statuses_batch(steam_ids) if steam_ids else {}
        for sid in steam_ids:
            # 三端 only：Steam / PSN / Xbox
            sp = split_platform_sid(str(sid))
            if sp and sp[0] == "nso":
                continue
            nt = next_poll.get(sid, now)
            sl = int(nt - now)
            p_str = f"下次轮询{sl}秒后" if sl < 60 else f"下次轮询{sl//60}分钟后"
            status = status_map.get(sid)
            if not status:
                continue
            name = status.get('name') or sid
            gameid = status.get('gameid')
            game = status.get('gameextrainfo')
            avatar_url = status.get('avatarfull') or status.get('avatar') or ''
            is_multi = bool(sp)
            if gameid and not is_multi:
                zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or "未知游戏")
            else:
                zh_game_name = game or ("未知游戏" if gameid else "")
            if gameid:
                st = self.session_service.started_at(group_id, sid, gameid)
                ps = now - st if st else 0
                pm = ps / 60
                ps_str = f"{pm:.1f}分钟" if pm < 60 else f"{pm/60:.1f}小时"
                user_list.append({'sid': sid, 'name': name, 'status': 'playing', 'avatar_url': avatar_url, 'game': zh_game_name, 'gameid': gameid, 'play_str': ps_str, 'group_id': group_id, 'poll_str': p_str})
            elif status.get('personastate', 0) > 0:
                p_status = _persona_status.get(status.get('personastate', 0), 'online')
                user_list.append({'sid': sid, 'name': name, 'status': p_status, 'avatar_url': avatar_url, 'game': '', 'gameid': '', 'play_str': '', 'group_id': group_id, 'poll_str': p_str})
            # 离线不加入 all 列表
        # 只保留 在玩 / 在线类
        _online_like = {'playing', 'online', 'busy', 'away', 'snooze'}
        user_list = [u for u in user_list if u.get('status') in _online_like]
        # 纯文本输出模式
        if mode.lower() == 'text':
            from ..presentation.renderers.steam_list import get_status_text
            if not user_list:
                yield event.plain_result("当前没有在玩或在线的玩家。")
                return
            lines = ["=== 本群在玩/在线 ===\n"]
            for u in user_list:
                sicon = {'playing': '🎮', 'online': '🔵', 'busy': '🔴', 'away': '🟣', 'snooze': '🟣'}.get(u['status'], '❓')
                name = u['name']
                stext = get_status_text(u['status'])
                detail = f" 正在玩：{u['game']}" if u['status'] == 'playing' and u.get('game') else ""
                play = f" | 时长：{u['play_str']}" if u.get('play_str') else ""
                lines.append(f"  {sicon} {name} {stext}{detail}{play}")
                lines.append(f"     ID: {u['sid']}")
            playing_n = sum(1 for u in user_list if u['status'] == 'playing')
            lines.append(f"📊 在玩 {playing_n} · 在线 {len(user_list) - playing_n}")
            yield event.plain_result("\n".join(lines))
            return
        # 图片输出模式（默认）：在玩 > 在线
        _status_rank = {'playing': 0, 'online': 1, 'busy': 2, 'away': 3, 'snooze': 4}
        user_list.sort(key=lambda u: _status_rank.get(u['status'], 9))
        if not user_list:
            yield event.plain_result("当前没有在玩或在线的玩家。")
            return
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        # 四平台分栏渲染（头像 72px，与 steam list 卡片模式一致；m 版头像图缩放清晰）
        from ..presentation.renderers.platform_list import render_platform_list_image

        async def cover_resolver(u):
            sp = split_platform_sid(str(u.get('sid', '')))
            if not sp and u.get('gameid'):
                return await self.get_game_cover_url(u['gameid'])
            return None

        img_path = await render_platform_list_image(
            user_list, font_path=font_path, proxy=self.proxy,
            cover_resolver=cover_resolver, data_dir=self.data_dir,
        )
        if img_path:
            yield event.image_result(img_path)
        else:
            yield event.plain_result("渲染图片失败")
        return
        avatar_frame_paths = {}
        for u in user_list:
            sid = u.get('sid', '')
            if sid:
                fp = await get_avatar_frame_path(self.data_dir, sid, proxy=self.proxy)
                if not fp:
                    frame_url = await get_avatar_frame_url(sid, proxy=self.proxy)
                    if frame_url:
                        fp = await get_avatar_frame_path(self.data_dir, sid, frame_url, proxy=self.proxy)
                if fp:
                    avatar_frame_paths[sid] = fp
        font_path = self.get_font_path('NotoSansHans-Regular.otf')
        # 新版steam风格不展示封面；旧版卡片风格需要封面，仅在关闭新风格时预取
        steam_style = self.config.get('enable_steam_style', False)
        covers = {}
        if not steam_style:
            for u in user_list:
                gid = u.get('gameid', '')
                if gid:
                    from ..presentation.renderers.game_start import get_cover_path
                    cp = await get_cover_path(
                        self.data_dir, gid, u.get('game', ''),
                        sgdb_api_key=self.SGDB_API_KEY,
                        appid=gid,
                        proxy=self.proxy,
                        sgdb_api_base=self.SGDB_API_BASE,
                    )
                    if cp:
                        covers[u['sid']] = cp
        img_bytes = await render_steam_list_image(self.data_dir, user_list, font_path=font_path, proxy=self.proxy, avatar_frame_paths=avatar_frame_paths, covers=covers, steam_style=steam_style)
        if img_bytes:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(img_bytes)
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
        else:
            yield event.plain_result("渲染图片失败")

    async def _push_targets_for_player(self, sid: str):
        """按玩家归属的群返回推送目标（unified_msg_origin）。"""
        umos = set()
        for gid, ids in self.group_steam_ids.items():
            if sid in ids:
                for s in self._get_notify_sessions(gid, sid):
                    umos.add(s)
        return umos

    def get_today_superpower(self, steamid):
        today = date.today().isoformat()
        cache_key = (steamid, today)
        if cache_key in self._superpower_cache:
            return self._superpower_cache[cache_key]
        if self._abilities is None:
            with open(self._abilities_path, encoding="utf-8") as abilities_file:
                self._abilities = [line.strip() for line in abilities_file if line.strip()]
        superpower = random.Random(f"{steamid}-{today}").choice(self._abilities)
        self._superpower_cache[cache_key] = superpower
        return superpower

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam push_group")
    async def steam_push_group(self, event: AstrMessageEvent, steamid: str):
        '''将本群加入指定玩家的联动推送组（不重复轮询，仅同步推送）'''
        group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sid = str(steamid).strip()
        sp = split_platform_sid(sid)
        if not sp and not (sid.isdigit() and len(sid) == 17):
            yield event.plain_result("ID无效，支持 17 位 SteamID64 或 psn:xxx / xbox:xxx")
            return
        found = False
        for gid, ids in self.group_steam_ids.items():
            if sid in ids:
                found = True
                break
        if not found:
            yield event.plain_result("未找到已轮询该玩家的主群，请先在任一群添加并开启监控。")
            return
        self.push_groups.setdefault(sid, [])
        if group_id not in self.push_groups[sid]:
            self.push_groups[sid].append(group_id)
            self._save_push_groups()
            yield event.plain_result(f"本群已加入 {sid} 的联动推送组。")
        else:
            yield event.plain_result("本群已在该玩家的推送组中。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("steam delpush_group")
    async def steam_delpush_group(self, event: AstrMessageEvent, steamid: str, target_group: str = ''):
        '''将当前群/指定群从玩家联动推送组移除'''
        if target_group:
            group_id = target_group.strip()
        else:
            group_id = str(event.get_group_id()) if hasattr(event, 'get_group_id') else 'default'
        sid = str(steamid).strip()
        sp = split_platform_sid(sid)
        if not sp and not (sid.isdigit() and len(sid) == 17):
            yield event.plain_result("ID无效，支持 17 位 SteamID64 或 psn:xxx / xbox:xxx")
            return
        if sid not in self.push_groups or group_id not in self.push_groups[sid]:
            yield event.plain_result(f"群 {group_id} 未在 {sid} 的推送组中。")
            return
        self.push_groups[sid].remove(group_id)
        if not self.push_groups[sid]:
            self.push_groups.pop(sid)
        self._save_push_groups()
        if target_group:
            yield event.plain_result(f"已从 {sid} 的联动推送组中移除群 {group_id}。")
        else:
            yield event.plain_result(f"本群已从 {sid} 的联动推送组移除。")
