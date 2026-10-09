"""多平台（PSN/Xbox/NSO）客户端 —— 提供与 Steam 同构的状态 dict。

状态 dict 字段与 Steam 保持一致（name/gameid/gameextrainfo/personastate/
lastlogoff/avatarfull/avatar），使得会话状态机 / 通知 / 智能轮询 / 排行
等应用层无需改动即可复用。

- PSN: 基于 psnawp（npsso 认证，同步库，asyncio.to_thread 包裹）
- Xbox: 基于 xbox-webapi（tokens 持久化 + refresh）——骨架
- NSO: 基于 nxapi HTTP 服务 —— 骨架
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ...shared.logging import logger

# 平台前缀：(前缀, 说明)
# nso 已禁用：任天堂 NSO API 域名（api.lp1.acs.nintendo.com / api.lp1.znc.srv）
# 于 2026-09 起公共 DNS NXDOMAIN，无法获取数据，禁止新增监控
NSO_DISABLED = True
PLATFORM_PREFIXES = ("psn", "xbox", "nso")


def split_platform_sid(sid: str) -> Optional[Tuple[str, str]]:
    """解析 'psn:xxxx' 形式的玩家键。返回 (platform, raw_id) 或 None。"""
    if not sid or ":" not in sid:
        return None
    platform, _, raw = sid.partition(":")
    if platform in PLATFORM_PREFIXES and raw:
        return platform, raw
    return None


class MultiPlatformClientMixin:
    """挂载到插件主类上的多平台状态获取能力。"""

    # ---- 配置（由 plugin 主类在 __init__ 中赋值） ----
    psn_npsso: str = ""
    xbox_config: Dict[str, Any] = {}
    nso_http_base: str = ""
    _psn_pool: Any = None
    _psn_last_fetch: float = 0.0

    async def fetch_multi_statuses(self, platform: str, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        """拉取指定平台的多个玩家状态，返回 {raw_id: 同构 status dict}。"""
        if platform == "psn":
            return await self._fetch_psn_statuses(ids)
        if platform == "xbox":
            return await self._fetch_xbox_statuses(ids)
        if platform == "nso":
            return await self._fetch_nso_statuses(ids)
        logger.warning(f"[MultiPlatform] 未知平台: {platform}")
        return {}

    # ---------- PSN ----------

    def _ensure_psnawp(self):
        import sys
        # psnawp 默认 DEBUG 会打印 token 到日志，静音避免泄漏
        try:
            import logging as _logging
            _logging.getLogger("psnawp_api").setLevel(_logging.WARNING)
            _logging.getLogger("urllib3").setLevel(_logging.WARNING)
        except Exception:
            pass
        # AstrBot 插件进程 sys.path 默认不含 data/site-packages，这里主动补上
        installed = False
        try:
            from psnawp_api import PSNAWP  # type: ignore
        except Exception:
            candidates = [
                os.path.join(os.getcwd(), "data", "site-packages"),
                "/AstrBot/data/site-packages",
            ]
            for c in candidates:
                if os.path.isdir(c) and c not in sys.path:
                    sys.path.insert(0, c)
            try:
                from psnawp_api import PSNAWP  # type: ignore
            except Exception:
                raise RuntimeError("psnawp 未安装：请执行 pip install psnawp（或重装插件）")
        if not self.psn_npsso:
            raise RuntimeError("PSN npsso 未配置")
        if self._psn_pool is None:
            self._psn_pool = PSNAWP(self.psn_npsso)
        return self._psn_pool

    async def _fetch_psn_statuses(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not self.psn_npsso:
            logger.warning("[MultiPlatform] PSN npsso 未配置，跳过 PSN 轮询")
            return {}
        # 串行化：轮询与 alllist 可能并发调用同一 psnawp 实例，避免竞态
        lock = getattr(self, "_psn_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._psn_lock = lock
        async with lock:
            return await self._fetch_psn_statuses_locked(ids)

    async def _fetch_psn_statuses_locked(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        # psnawp 基于 requests（trust_env 读取代理环境变量）；
        # 设置进程级代理使 PSN 请求经 mihomo（白名单外域名自动直连，无副作用）
        if self.proxy:
            os.environ.setdefault("HTTPS_PROXY", self.proxy)
            os.environ.setdefault("HTTP_PROXY", self.proxy)
        # psnawp 内置限速（300 请求/15 分钟），再叠加大间隔保守调用
        wait = 8.0 - (time.time() - self._psn_last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            client = self._ensure_psnawp()
        except Exception as e:
            logger.error(f"[MultiPlatform] PSN 初始化失败: {e}")
            return {}
        logger.info(f"[PSN] 开始拉取 {ids} (pool={'reuse' if self._psn_pool else 'fresh'})")
        result: Dict[str, Dict[str, Any]] = {}
        for online_id in ids:
            def _get():
                try:
                    return client.user(online_id=online_id)
                except Exception as e:
                    raise e
            try:
                user = await asyncio.to_thread(_get)
            except Exception as e:
                logger.warning(f"[PSN] user 获取失败 {online_id}: {type(e).__name__} {str(e)[:120]}")
                user = None
            def _avatar(u=user):
                try:
                    p = u.profile()
                    if isinstance(p, dict):
                        # psnawp 3.x 聚合结构：自定义头像在 personalDetail.profilePictures，
                        # 默认头像在顶层 avatars。统一取 m 尺寸（160px 大图），
                        # 与 steam list 缓存的 sid.jpg 同源；小尺寸显示保留细节。
                        # URL 统一转 https（http 明文拉取不稳）。
                        def _pick(arr, prefer="m"):
                            by_size = {}
                            for item in arr or []:
                                if not isinstance(item, dict):
                                    continue
                                url = item.get("url")
                                if not url:
                                    continue
                                url = url.replace("http://", "https://")
                                s = str(item.get("size", "")).lower()
                                by_size.setdefault(s, url)
                            for key in (prefer, "l", "xl", "s", "m"):
                                if by_size.get(key):
                                    return by_size[key]
                            return None
                        pd = p.get("personalDetail") or {}
                        url = _pick(pd.get("profilePictures")) or _pick(p.get("profilePictures"))
                        if url:
                            return url
                        url = _pick(p.get("avatars"))
                        if url:
                            return url
                        return p.get("avatarUrl") or p.get("avatar")
                    return getattr(p, "avatarUrl", None) or getattr(p, "avatar", None)
                except Exception:
                    return None
            avatar_url = await asyncio.to_thread(_avatar)
            def _presence(u=user):
                try:
                    return u.get_presence()
                except Exception:
                    return None
            presence = await asyncio.to_thread(_presence)
            if not isinstance(presence, dict):
                continue
            # psnawp 3.x: presence 为 dict（basicPresence.primaryPlatformInfo.onlineStatus / basicGame）
            basic = presence.get("basicPresence") or {}
            pinfo = basic.get("primaryPlatformInfo") or {}
            online_status = (pinfo.get("onlineStatus") or presence.get("isOnline") or "").lower()
            if online_status in ("offline", "unavailable"):  # 无在线状态（离线/隐身）
                # 从 lastAvailableDate 解析上次在线时间，供「上次在线 X 小时前」展示
                last_online = basic.get("lastAvailableDate") or pinfo.get("lastOnlineDate")
                lastlogoff = None
                if last_online:
                    try:
                        from datetime import datetime
                        lastlogoff = int(
                            datetime.fromisoformat(str(last_online).replace("Z", "+00:00")).timestamp()
                        )
                    except Exception:
                        lastlogoff = None
                result[online_id] = {
                    "name": online_id,
                    "gameid": None,
                    "gameextrainfo": None,
                    "personastate": 0,
                    "lastlogoff": lastlogoff,
                    "avatarfull": avatar_url,
                    "avatar": avatar_url,
                    "platform": "psn",
                }
                continue
            base_game = presence.get("basicGame") or {}
            game_name = (
                presence.get("gameName")
                or base_game.get("gameName")
                or presence.get("game")
            )
            game_id = (
                presence.get("titleId")
                or presence.get("gameTitleId")
                or base_game.get("titleId")
            )
            cover_url = (
                presence.get("gameImage")
                or presence.get("gameImageUrl")
                or base_game.get("imageUrl")
                or base_game.get("gameImage")
            )
            result[online_id] = {
                "name": online_id,
                "gameid": str(game_id) if game_id else None,
                "gameextrainfo": game_name if game_name else None,
                "personastate": 1,
                "lastlogoff": None,
                "avatarfull": avatar_url,
                "avatar": avatar_url,
                "platform": "psn",
                "cover_url": cover_url,
            }
        self._psn_last_fetch = time.time()
        logger.info(f"[PSN] 拉取完成 {len(result)}/{len(ids)} 名玩家")
        return result

    # ---------- Xbox（xbox-webapi + tokens.json） ----------

    # Xbox App 公共客户端 ID（OpenXbox / xbox-authenticate 默认；未配置 client_id 时使用）
    _XBOX_DEFAULT_CLIENT_ID = "0000000048093EE3"
    _xbox_client: Any = None
    _xbox_auth: Any = None
    _xbox_session: Any = None
    _xbox_xuid_cache: Dict[str, Tuple[str, float]] = {}  # gamertag_lower -> (xuid, ts)
    _xbox_profile_cache: Dict[str, Tuple[Dict[str, Any], float]] = {}  # xuid -> (profile, ts)

    def _xbox_tokens_path(self) -> str:
        configured = (self.xbox_config.get("tokens_file") or "").strip()
        if configured:
            return configured
        data_dir = getattr(self, "data_dir", None) or os.path.join(
            "data", "plugin_data", "steam_status_monitor_V3"
        )
        return os.path.join(data_dir, "xbox_tokens.json")

    def _xbox_configured(self) -> bool:
        return bool(self._xbox_tokens_path() and os.path.isfile(self._xbox_tokens_path()))

    def _ensure_xbox_webapi(self):
        """惰性导入 xbox-webapi，兼容 AstrBot site-packages。"""
        import sys
        try:
            from xbox.webapi.api.client import XboxLiveClient  # type: ignore
            return True
        except Exception:
            candidates = [
                os.path.join(os.getcwd(), "data", "site-packages"),
                "/AstrBot/data/site-packages",
            ]
            for c in candidates:
                if os.path.isdir(c) and c not in sys.path:
                    sys.path.insert(0, c)
            try:
                from xbox.webapi.api.client import XboxLiveClient  # type: ignore  # noqa: F401
                return True
            except Exception:
                return False

    async def _ensure_xbox_client(self):
        """加载/刷新 Xbox tokens，返回 XboxLiveClient。刷新串行化，避免并发用掉同一 refresh_token。"""
        fail_until = float(getattr(self, "_xbox_auth_fail_until", 0) or 0)
        if fail_until and time.time() < fail_until:
            raise RuntimeError(
                f"Xbox token 暂时不可用（冷却中，剩余约 {int(fail_until - time.time())} 秒），"
                "请重新 xbox-authenticate 并上传 tokens.json"
            )
        if self._xbox_client is not None:
            return self._xbox_client
        lock = getattr(self, "_xbox_init_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._xbox_init_lock = lock
        async with lock:
            if self._xbox_client is not None:
                return self._xbox_client
            if not self._ensure_xbox_webapi():
                raise RuntimeError("xbox-webapi 未安装：请在 AstrBot 环境 pip install xbox-webapi")
            tokens_path = self._xbox_tokens_path()
            if not os.path.isfile(tokens_path):
                raise RuntimeError(
                    f"Xbox tokens 文件不存在: {tokens_path}。"
                    "请在本机执行 xbox-authenticate 生成 tokens.json 后放到该路径"
                )
            from xbox.webapi.authentication.manager import AuthenticationManager  # type: ignore
            from xbox.webapi.authentication.models import OAuth2TokenResponse  # type: ignore
            from xbox.webapi.common.signed_session import SignedSession  # type: ignore
            from xbox.webapi.api.client import XboxLiveClient  # type: ignore

            client_id = (self.xbox_config.get("client_id") or "").strip() or self._XBOX_DEFAULT_CLIENT_ID
            client_secret = (self.xbox_config.get("client_secret") or "").strip()
            session = SignedSession()
            if self.proxy:
                os.environ.setdefault("HTTPS_PROXY", self.proxy)
                os.environ.setdefault("HTTP_PROXY", self.proxy)
            auth = AuthenticationManager(session, client_id, client_secret, "")
            with open(tokens_path, encoding="utf-8") as f:
                auth.oauth = OAuth2TokenResponse.model_validate_json(f.read())
            try:
                await auth.refresh_tokens()
            except Exception as exc:
                # token 失效：冷却一段时间再重试，避免每分钟刷屏
                fail_until = time.time() + 1800
                self._xbox_auth_fail_until = fail_until
                logger.warning(
                    f"[Xbox] token 刷新失败（400/过期等），30 分钟内不再自动重试。"
                    f"请本机 xbox-authenticate 后上传 tokens.json。详情: {exc}"
                )
                raise RuntimeError(
                    f"Xbox token 刷新失败（可能已过期，需重新 xbox-authenticate）: {exc}"
                ) from exc
            try:
                with open(tokens_path, "w", encoding="utf-8") as f:
                    if hasattr(auth.oauth, "model_dump_json"):
                        f.write(auth.oauth.model_dump_json())
                    else:
                        f.write(auth.oauth.json())
            except Exception as exc:
                logger.warning(f"[Xbox] tokens 回写失败（不影响本次调用）: {exc}")
            client = XboxLiveClient(auth)
            self._xbox_session = session
            self._xbox_auth = auth
            self._xbox_client = client
            logger.info(f"[Xbox] 客户端就绪 (tokens={tokens_path})")
            return client

    def _reset_xbox_client(self):
        self._xbox_client = None
        self._xbox_auth = None
        self._xbox_session = None

    @staticmethod
    def _is_xuid(raw: str) -> bool:
        s = str(raw or "").strip()
        return s.isdigit() and 12 <= len(s) <= 20

    async def _xbox_resolve_xuid(self, client, raw_id: str) -> Optional[str]:
        raw = str(raw_id or "").strip()
        if not raw:
            return None
        if self._is_xuid(raw):
            return raw
        key = raw.casefold()
        cached = self._xbox_xuid_cache.get(key)
        now = time.time()
        if cached and now - cached[1] < 86400:
            return cached[0]
        try:
            profile = await client.profile.get_profile_by_gamertag(raw)
        except Exception as exc:
            logger.warning(f"[Xbox] Gamertag 解析失败 {raw}: {type(exc).__name__} {str(exc)[:120]}")
            return None
        xuid = None
        # ProfileResponse: profileUsers[].id；兼容 dict / pydantic
        data = profile
        if hasattr(data, "model_dump"):
            data = data.model_dump(by_alias=True)
        if isinstance(data, dict):
            users = data.get("profileUsers") or data.get("profile_users") or []
            if users:
                first = users[0]
                if hasattr(first, "model_dump"):
                    first = first.model_dump(by_alias=True)
                if isinstance(first, dict):
                    xuid = str(first.get("id") or first.get("xuid") or "")
        elif isinstance(data, list) and data:
            item = data[0]
            if hasattr(item, "model_dump"):
                item = item.model_dump(by_alias=True)
            if isinstance(item, dict):
                xuid = str(item.get("id") or item.get("xuid") or "")
        if xuid and xuid.isdigit():
            self._xbox_xuid_cache[key] = (xuid, now)
            return xuid
        return None

    async def _xbox_profile_bits(self, client, xuid: str) -> Dict[str, Any]:
        """取显示名与头像 URL（带缓存）。"""
        now = time.time()
        cached = self._xbox_profile_cache.get(xuid)
        if cached and now - cached[1] < 86400:
            return cached[0]
        bits: Dict[str, Any] = {"name": "", "avatar": ""}
        data = None
        try:
            # xbox-webapi 2.x: get_profile_by_xuid
            if hasattr(client.profile, "get_profile_by_xuid"):
                data = await client.profile.get_profile_by_xuid(xuid)
            elif hasattr(client.profile, "get_profile_with_settings"):
                data = await client.profile.get_profile_with_settings(xuid)
        except Exception as exc:
            logger.debug(f"[Xbox] profile 获取失败 {xuid}: {exc}")
            data = None
        if hasattr(data, "model_dump"):
            try:
                data = data.model_dump(by_alias=True)
            except Exception:
                data = None
        settings = {}
        if isinstance(data, dict):
            users = data.get("profileUsers") or data.get("profile_users") or []
            if users:
                user0 = users[0]
                if hasattr(user0, "model_dump"):
                    user0 = user0.model_dump(by_alias=True)
                for s in (user0.get("settings") or []) if isinstance(user0, dict) else []:
                    if hasattr(s, "model_dump"):
                        s = s.model_dump(by_alias=True)
                    if isinstance(s, dict) and s.get("id"):
                        settings[str(s["id"])] = s.get("value")
            elif isinstance(data.get("settings"), list):
                for s in data["settings"]:
                    if isinstance(s, dict) and s.get("id"):
                        settings[str(s["id"])] = s.get("value")
            else:
                for k, v in data.items():
                    if isinstance(v, (str, int, float, bool)):
                        settings[str(k)] = v
        name = (
            settings.get("ModernGamertag")
            or settings.get("Gamertag")
            or settings.get("modernGamertag")
            or settings.get("gamertag")
            or ""
        )
        avatar = (
            settings.get("GameDisplaypicRaw")
            or settings.get("GameDisplayPicRaw")
            or settings.get("displayPicRaw")
            or ""
        )
        if isinstance(avatar, str) and avatar.startswith("http://"):
            avatar = "https://" + avatar[len("http://"):]
        bits = {"name": str(name or ""), "avatar": str(avatar or "")}
        self._xbox_profile_cache[xuid] = (bits, now)
        return bits

    @staticmethod
    def _xbox_presence_to_status(
        presence: Any,
        raw_id: str,
        xuid: str,
        profile: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Xbox presence → Steam 同构 status dict。"""
        name = profile.get("name") or raw_id
        avatar = profile.get("avatar") or None
        base = {
            "name": name,
            "gameid": None,
            "gameextrainfo": None,
            "personastate": 0,
            "lastlogoff": None,
            "avatarfull": avatar,
            "avatar": avatar,
            "platform": "xbox",
            "xuid": xuid,
        }
        # 兼容 pydantic 模型 / dict
        if hasattr(presence, "model_dump"):
            try:
                presence = presence.model_dump(by_alias=True)
            except Exception:
                try:
                    presence = presence.model_dump()
                except Exception:
                    presence = None
        elif hasattr(presence, "dict"):
            try:
                presence = presence.dict(by_alias=True)
            except Exception:
                presence = None
        if not isinstance(presence, dict):
            return base
        state = str(presence.get("state") or presence.get("presenceState") or "").strip()
        online = state.lower() in ("online", "playing", "idle", "blocked", "donotdisturb", "away")
        # focused title：placement=Full 且 state=Active 优先
        game_name = None
        game_id = None
        cover = None
        devices = presence.get("devices") or []
        if not isinstance(devices, list):
            devices = []
        candidates = []
        for device in devices:
            if not isinstance(device, dict):
                continue
            titles = device.get("titles") or []
            if not isinstance(titles, list):
                continue
            for title in titles:
                if not isinstance(title, dict):
                    continue
                t_state = str(title.get("state") or "").lower()
                if t_state and t_state not in ("active", "gamingsnap"):
                    continue
                # 过滤系统应用（设置/主页等）：无 name 或 id 很小且常见系统名
                t_name = title.get("name") or title.get("titleName")
                if not t_name:
                    continue
                placement = str(title.get("placement") or "").lower()
                score = 2 if placement == "full" else (1 if placement in ("fill", "background") else 0)
                candidates.append((score, str(t_name), str(title.get("id") or ""), title))
        if candidates:
            candidates.sort(key=lambda x: x[0], reverse=True)
            # 忽略明显系统壳（Dashboard/Home）除非只有一项
            skip_names = {
                "home", "xbox", "settings", "settings.exe", "system",
                "xbox app", "game pass", "microsoft store",
            }
            chosen = None
            for score, t_name, t_id, raw in candidates:
                if t_name.casefold() in skip_names and len(candidates) > 1:
                    continue
                chosen = (t_name, t_id, raw)
                break
            if chosen is None and candidates:
                chosen = (candidates[0][1], candidates[0][2], candidates[0][3])
            if chosen:
                game_name, game_id, raw = chosen
                cover = raw.get("displayImage") or raw.get("display_icon")
        if online:
            if game_name or game_id:
                base["personastate"] = 1
                base["gameextrainfo"] = game_name
                base["gameid"] = str(game_id) if game_id else None
                if cover:
                    if isinstance(cover, str) and cover.startswith("http://"):
                        cover = "https://" + cover[len("http://"):]
                    base["cover_url"] = cover
            else:
                base["personastate"] = 1  # 在线但未在游戏（Steam 2=busy，勿用）
        return base

    async def _fetch_xbox_statuses(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        if not self._xbox_configured() and not self.xbox_config.get("client_id"):
            logger.warning("[MultiPlatform] Xbox 凭据未配置（缺 tokens.json），跳过 Xbox 轮询")
            return {}
        if not self._xbox_configured():
            logger.warning("[MultiPlatform] Xbox tokens.json 不存在，跳过 Xbox 轮询")
            return {}
        lock = getattr(self, "_xbox_lock", None)
        if lock is None:
            lock = asyncio.Lock()
            self._xbox_lock = lock
        async with lock:
            return await self._fetch_xbox_statuses_locked(ids)

    async def _fetch_xbox_statuses_locked(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            msg = str(e)
            # 冷却中属预期状态（token 过期后 30 分钟内），降级 debug 避免每轮 ERROR 刷屏
            if "冷却中" in msg or "暂时不可用" in msg:
                logger.debug(f"[Xbox] 初始化跳过（冷却中）: {msg}")
            else:
                logger.warning(f"[Xbox] 初始化失败: {msg}")
            return {}
        logger.info(f"[Xbox] 开始拉取 {ids}")
        result: Dict[str, Dict[str, Any]] = {}
        # 1) 解析 xuid
        id_to_xuid: Dict[str, str] = {}
        for raw_id in ids:
            xuid = await self._xbox_resolve_xuid(client, raw_id)
            if xuid:
                id_to_xuid[raw_id] = xuid
            else:
                logger.warning(f"[Xbox] 无法解析玩家 {raw_id}")
        if not id_to_xuid:
            return {}
        # 2) 批量 presence
        xuids = list(dict.fromkeys(id_to_xuid.values()))
        presences: Dict[str, Any] = {}
        try:
            # level=title 以拿到当前游戏 title 信息
            try:
                from xbox.webapi.api.provider.presence import PresenceLevel  # type: ignore
                level = PresenceLevel.TITLE
            except Exception:
                level = "title"
            batch = await client.presence.get_presence_batch(xuids, presence_level=level)
            if isinstance(batch, list):
                for item in batch:
                    if not isinstance(item, dict):
                        # pydantic 模型
                        if hasattr(item, "model_dump"):
                            item = item.model_dump()
                        elif hasattr(item, "dict"):
                            item = item.dict()
                        else:
                            continue
                    x = str(item.get("xuid") or "")
                    if x:
                        presences[x] = item
            elif isinstance(batch, dict):
                # 有的版本直接返回 {xuid: presence} 或单条 presence
                if "xuid" in batch:
                    presences[str(batch["xuid"])] = batch
                else:
                    for x, item in batch.items():
                        if isinstance(item, dict):
                            presences[str(x)] = item
        except Exception as e:
            msg = str(e)
            if any(k in msg.lower() for k in ("401", "unauthorized", "token", "xsts", "expired")):
                self._reset_xbox_client()
                logger.warning("[Xbox] 认证失效，已重置客户端，下轮将重新 refresh tokens")
            logger.warning(f"[Xbox] presence 批量拉取失败，降级为单查: {type(e).__name__} {msg[:140]}")
        # 单查补齐
        for raw_id, xuid in id_to_xuid.items():
            if xuid not in presences:
                try:
                    try:
                        from xbox.webapi.api.provider.presence import PresenceLevel  # type: ignore
                        level = PresenceLevel.TITLE
                    except Exception:
                        level = "title"
                    one = await client.presence.get_presence(xuid, presence_level=level)
                    if hasattr(one, "model_dump"):
                        one = one.model_dump(by_alias=True)
                    elif hasattr(one, "dict"):
                        one = one.dict(by_alias=True)
                    if isinstance(one, dict):
                        presences[xuid] = one
                except Exception as e:
                    logger.warning(f"[Xbox] presence 单查失败 {raw_id}/{xuid}: {type(e).__name__} {str(e)[:120]}")
        # 3) 组装同构 status
        for raw_id, xuid in id_to_xuid.items():
            profile = await self._xbox_profile_bits(client, xuid)
            pres = presences.get(xuid)
            result[raw_id] = self._xbox_presence_to_status(pres, raw_id, xuid, profile)
        logger.info(f"[Xbox] 拉取完成 {len(result)}/{len(ids)} 名玩家")
        return result

    _xbox_title_cover_cache: Dict[str, Tuple[Optional[str], float]] = {}

    async def fetch_xbox_title_cover(self, title_id: str) -> Optional[str]:
        """通过 Titlehub 获取 Xbox title 的封面图 URL（缓存 7 天）。"""
        tid = str(title_id or "").strip()
        if not tid:
            return None
        now = time.time()
        cached = self._xbox_title_cover_cache.get(tid)
        if cached and now - cached[1] < 7 * 86400:
            return cached[0]
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            logger.warning(f"[Xbox] title 封面客户端失败: {e}")
            return None
        url = None
        try:
            try:
                from xbox.webapi.api.provider.titlehub import TitleFields  # type: ignore
                fields = [TitleFields.IMAGE, TitleFields.DETAIL]
            except Exception:
                fields = None
            info = await client.titlehub.get_title_info(tid, fields=fields)
            data = info
            if hasattr(data, "model_dump"):
                data = data.model_dump(by_alias=True)
            titles = []
            if isinstance(data, dict):
                titles = data.get("titles") or []
            elif hasattr(data, "titles"):
                titles = data.titles or []
            title0 = titles[0] if titles else None
            if hasattr(title0, "model_dump"):
                title0 = title0.model_dump(by_alias=True)
            if isinstance(title0, dict):
                url = title0.get("displayImage") or title0.get("display_image")
                if not url:
                    for img in title0.get("images") or []:
                        if hasattr(img, "model_dump"):
                            img = img.model_dump(by_alias=True)
                        if isinstance(img, dict) and img.get("url"):
                            # 优先 BoxArt / Logo 等竖版
                            t = str(img.get("type") or "").lower()
                            url = img.get("url")
                            if "box" in t or "tile" in t:
                                break
        except Exception as e:
            logger.warning(f"[Xbox] title 封面查询失败 {tid}: {type(e).__name__} {str(e)[:120]}")
        if isinstance(url, str) and url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        self._xbox_title_cover_cache[tid] = (url, now)
        if url:
            logger.info(f"[Xbox] title {tid} 封面: {url[:80]}")
        return url

    _name_cover_cache: Dict[str, Tuple[Optional[str], float]] = {}

    async def resolve_cover_by_game_name(self, game_name: str) -> Optional[str]:
        """多平台兜底：按游戏名解析封面 URL。
        优先 Steam 商店 appdetails/header + library capsule，再 SGDB。"""
        name = str(game_name or "").strip()
        if not name:
            return None
        now = time.time()
        cached = self._name_cover_cache.get(name.casefold())
        if cached and now - cached[1] < 7 * 86400:
            return cached[0]
        url = None
        try:
            import httpx
            from ...shared.network import httpx_client_kwargs, shared_httpx_client
            from urllib.parse import quote
            async with shared_httpx_client(proxy=self.proxy, timeout=15, follow_redirects=False) as client:
                # 1) Steam storesearch → appid
                appid = None
                try:
                    r = await client.get(
                        "https://store.steampowered.com/api/storesearch/",
                        params={"term": name, "l": "english", "cc": "us"},
                    )
                    items = (r.json() or {}).get("items") or []
                    if items:
                        appid = items[0].get("id")
                except Exception as e:
                    logger.debug(f"[Cover] storesearch 失败 {name}: {e}")
                # 2) appdetails 取 header（横版兜底）并尝试 library 竖版
                if appid:
                    try:
                        r2 = await client.get(
                            "https://store.steampowered.com/api/appdetails",
                            params={"appids": str(appid), "l": "english"},
                        )
                        data = (r2.json() or {}).get(str(appid)) or {}
                        dd = data.get("data") or {}
                        header = dd.get("header_image")
                        # Steam library 竖版：IStoreBrowse 需要 key；无 key 时用 capsule 横版也可渲染
                        url = header
                    except Exception as e:
                        logger.debug(f"[Cover] appdetails 失败 appid={appid}: {e}")
                # 3) SGDB 竖版
                sgdb_key = getattr(self, "SGDB_API_KEY", "") or ""
                if sgdb_key:
                    try:
                        headers = {"Authorization": f"Bearer {sgdb_key}"}
                        base = (getattr(self, "SGDB_API_BASE", "") or "https://www.steamgriddb.com").rstrip("/")
                        sr = await client.get(
                            f"{base}/api/v2/search/autocomplete/{quote(name)}",
                            headers=headers,
                        )
                        sdata = sr.json() or {}
                        if sdata.get("data"):
                            gid = sdata["data"][0]["id"]
                            gr = await client.get(
                                f"{base}/api/v2/grids/game/{gid}",
                                params={"dimensions": "600x900", "type": "static", "limit": 1},
                                headers=headers,
                            )
                            gdata = gr.json() or {}
                            if gdata.get("data"):
                                url = gdata["data"][0].get("url") or url
                    except Exception as e:
                        logger.debug(f"[Cover] SGDB 失败 {name}: {e}")
        except Exception as e:
            logger.warning(f"[Cover] 按名称解析封面失败 {name}: {e}")
        if isinstance(url, str) and url.startswith("http://"):
            url = "https://" + url[len("http://"):]
        self._name_cover_cache[name.casefold()] = (url, now)
        if url:
            logger.info(f"[Cover] 名称解析封面 {name}: {url[:90]}")
        return url

    async def fetch_xbox_debug_status(self, raw_id: str) -> str:
        """管理员调试：拉单个 Xbox 玩家并返回可读摘要。"""
        if not self._xbox_configured():
            return f"未配置 tokens.json（期望路径: {self._xbox_tokens_path()}）"
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            return f"客户端初始化失败: {e}"
        xuid = await self._xbox_resolve_xuid(client, raw_id)
        if not xuid:
            return f"无法解析 {raw_id}（Gamertag 或 XUID 无效）"
        profile = await self._xbox_profile_bits(client, xuid)
        try:
            pres = await client.presence.get_presence(xuid)
            if hasattr(pres, "model_dump"):
                pres = pres.model_dump()
            elif hasattr(pres, "dict"):
                pres = pres.dict()
        except Exception as e:
            return f"xuid={xuid} name={profile.get('name')} avatar={'有' if profile.get('avatar') else '无'}；presence 失败: {e}"
        st = self._xbox_presence_to_status(pres, raw_id, xuid, profile)
        return (
            f"xuid={xuid} name={st.get('name')} state={st.get('personastate')} "
            f"game={st.get('gameextrainfo')} gameid={st.get('gameid')} "
            f"avatar={'有' if st.get('avatarfull') else '无'}"
        )

    # ---------- Xbox 成就 ----------

    _xbox_ach_cache: Dict[str, Tuple[Optional[set], float]] = {}
    _xbox_ach_details_cache: Dict[str, Tuple[Optional[Dict[str, Any]], float]] = {}

    def _xbox_to_dict(self, obj: Any) -> Any:
        if hasattr(obj, "model_dump"):
            try:
                return obj.model_dump(by_alias=True)
            except Exception:
                try:
                    return obj.model_dump()
                except Exception:
                    return obj
        return obj

    async def fetch_xbox_title_achievements(self, raw_id: str, title_id: str) -> Optional[set]:
        """返回该 Xbox 玩家在 title 上已解锁成就名集合；失败返回 None。"""
        key = f"{raw_id}:{title_id}"
        now = time.time()
        cached = self._xbox_ach_cache.get(key)
        if cached and now - cached[1] < 600:
            return cached[0]
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            logger.warning(f"[Xbox成就] 客户端失败: {e}")
            return None
        xuid = await self._xbox_resolve_xuid(client, raw_id)
        if not xuid:
            return None
        try:
            resp = await client.achievements.get_achievements_xboxone_gameprogress(xuid, title_id)
            data = self._xbox_to_dict(resp)
            items = (data or {}).get("achievements") or []
            unlocked = set()
            for a in items:
                if not isinstance(a, dict):
                    continue
                if str(a.get("progressState") or a.get("progress_state") or "").lower() in (
                    "achieved", "earned", "unlocked",
                ):
                    unlocked.add(str(a.get("name") or a.get("id") or ""))
            self._xbox_ach_cache[key] = (unlocked, now)
            logger.info(f"[Xbox成就] {raw_id} title={title_id} 已解锁 {len(unlocked)}")
            return unlocked
        except Exception as e:
            logger.warning(f"[Xbox成就] gameprogress 失败 {raw_id}/{title_id}: {type(e).__name__} {str(e)[:120]}")
            return None

    async def fetch_xbox_achievement_details(self, raw_id: str, title_id: str, game_name: str = "") -> Optional[Dict[str, Any]]:
        """返回与 Steam 同构的成就详情 dict：{name: {name, desc, unlocked, icon, game_name}}"""
        key = f"{raw_id}:{title_id}"
        now = time.time()
        cached = self._xbox_ach_details_cache.get(key)
        if cached and now - cached[1] < 3600:
            return cached[0]
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            logger.warning(f"[Xbox成就] 详情客户端失败: {e}")
            return None
        xuid = await self._xbox_resolve_xuid(client, raw_id)
        if not xuid:
            return None
        try:
            resp = await client.achievements.get_achievements_xboxone_gameprogress(xuid, title_id)
            data = self._xbox_to_dict(resp)
            items = (data or {}).get("achievements") or []
            details: Dict[str, Any] = {}
            for a in items:
                if not isinstance(a, dict):
                    continue
                name = str(a.get("name") or a.get("id") or "")
                if not name:
                    continue
                state = str(a.get("progressState") or a.get("progress_state") or "").lower()
                unlocked = state in ("achieved", "earned", "unlocked")
                desc = a.get("description") if unlocked else (a.get("lockedDescription") or a.get("locked_description") or "")
                icon = ""
                for m in a.get("mediaAssets") or a.get("media_assets") or []:
                    if isinstance(m, dict) and m.get("url"):
                        icon = m.get("url")
                        if str(m.get("type") or "").lower() in ("image", "icon"):
                            break
                details[name] = {
                    "name": name,
                    "desc": desc or "",
                    "unlocked": unlocked,
                    "icon": icon,
                    "game_name": game_name or "",
                }
            self._xbox_ach_details_cache[key] = (details, now)
            return details
        except Exception as e:
            logger.warning(f"[Xbox成就] 详情失败 {raw_id}/{title_id}: {type(e).__name__} {str(e)[:120]}")
            return None

    async def fetch_xbox_recent_unlocked_titles(self, raw_id: str, limit: int = 5) -> List[Dict[str, Any]]:
        """最近有成就解锁的 title 列表（调试用）。"""
        import traceback
        try:
            client = await self._ensure_xbox_client()
        except Exception as e:
            logger.warning(f"[Xbox成就] recent 客户端失败 {raw_id}: {e}")
            return []
        xuid = await self._xbox_resolve_xuid(client, raw_id)
        if not xuid:
            logger.warning(f"[Xbox成就] recent 无法解析 xuid: {raw_id}")
            return []
        # 1) achievements history API
        try:
            resp = await client.achievements.get_achievements_xboxone_recent_progress_and_info(xuid)
            data = self._xbox_to_dict(resp) or {}
            out = []
            for t in (data.get("titles") or [])[:limit]:
                if isinstance(t, dict):
                    out.append({
                        "titleId": t.get("titleId"),
                        "name": t.get("name"),
                        "earned": t.get("earnedAchievements"),
                        "lastUnlock": t.get("lastUnlock"),
                        "scid": t.get("serviceConfigId"),
                    })
            if out:
                return out
        except Exception as e:
            logger.warning(
                f"[Xbox成就] recent API 失败 {raw_id}/{xuid}: {type(e).__name__}: {e}\n{traceback.format_exc()}"
            )
        # 2) 降级：titlehub 最近游玩（成就数可能为 0，仍可作 title 候选）
        try:
            hist = await client.titlehub.get_title_history(xuid, max_items=limit)
            data = self._xbox_to_dict(hist) or {}
            out = []
            for t in (data.get("titles") or [])[:limit]:
                if not isinstance(t, dict):
                    continue
                ach = t.get("achievement") or {}
                out.append({
                    "titleId": t.get("titleId"),
                    "name": t.get("name"),
                    "earned": (ach.get("currentAchievements") if isinstance(ach, dict) else None),
                    "lastUnlock": t.get("titleHistory", {}).get("lastTimePlayed") if isinstance(t.get("titleHistory"), dict) else None,
                    "scid": t.get("serviceConfigId"),
                })
            return out
        except Exception as e:
            logger.warning(f"[Xbox成就] titlehub 降级失败 {raw_id}: {type(e).__name__}: {e}")
            return []

    # ---------- NSO（已禁用：上游 API 域名 NXDOMAIN，拿不到数据） ----------

    async def _fetch_nso_statuses(self, ids: List[str]) -> Dict[str, Dict[str, Any]]:
        # 静默跳过，避免每轮刷 warning；API 恢复前不接入
        return {}
