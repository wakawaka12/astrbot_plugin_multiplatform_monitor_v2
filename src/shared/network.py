"""插件网络客户端的 TLS 与代理公共配置。"""
import asyncio
import os
import ssl
import time
from contextlib import asynccontextmanager

import httpx


_ssl_ca_file = ""
_ssl_context = None
# 进程级共享连接池：key = proxy 字符串（空串表示直连）
_shared_clients: dict = {}
_zombie_clients: list = []


def configure_tls(ssl_ca_file=None):
    """配置可信自定义 CA；空值表示继续使用系统默认信任链。"""
    global _ssl_ca_file, _ssl_context
    raw_path = str(ssl_ca_file or "").strip()
    path = os.path.abspath(os.path.expanduser(raw_path)) if raw_path else ""
    if path and not os.path.isfile(path):
        raise ValueError(f"自定义 CA 文件不存在: {path}")
    _ssl_ca_file = path
    _ssl_context = ssl.create_default_context(cafile=path) if path else None
    # TLS 变更后旧连接不可信，池内客户端下次使用前会被替换
    if _shared_clients:
        _zombie_clients.extend(_shared_clients.values())
        _shared_clients.clear()


def get_ssl_ca_file():
    return _ssl_ca_file


def httpx_client_kwargs(proxy=None):
    """所有 httpx 客户端统一入口：代理 + 可选自定义 CA。

    - proxy 为配置里的代理（如 http://172.17.0.1:11080）
    - 始终显式带上 proxy 键，避免有的调用漏传导致商店/CDN 直连超时
    """
    kwargs = {"proxy": proxy}
    if _ssl_context is not None:
        kwargs["verify"] = _ssl_context
    return kwargs


class _SharedClientView:
    """池化客户端的视图：按调用方原 timeout/follow_redirects 发请求，退出时不关闭底层连接。"""

    __slots__ = ("_raw", "_timeout", "_follow_redirects")

    def __init__(self, raw: httpx.AsyncClient, timeout, follow_redirects: bool = False):
        object.__setattr__(self, "_raw", raw)
        object.__setattr__(self, "_timeout", timeout)
        object.__setattr__(self, "_follow_redirects", follow_redirects)

    @property
    def is_closed(self) -> bool:
        return self._raw.is_closed

    async def aclose(self):
        """视图上的 aclose 为 no-op，避免误关进程级连接池。"""
        return None

    def _wrap(self, attr):
        def _call(*args, **kwargs):
            kwargs.setdefault("timeout", self._timeout)
            kwargs.setdefault("follow_redirects", self._follow_redirects)
            return attr(*args, **kwargs)

        return _call

    def __getattr__(self, name):
        attr = getattr(self._raw, name)
        if name in {
            "get", "post", "put", "patch", "delete", "head", "options",
            "request", "stream", "send", "build_request",
        }:
            return self._wrap(attr)
        return attr


# 状态推送/轮询专用连接池：与查价/愿望单/爬虫隔离，扫描期间也要能推上下线
_status_clients: dict = {}
_STATUS_POOL_FLAG = None  # None=用文件探测；True/False=强制
_STATUS_POOL_MAX = 50
# 同时允许进入状态请求的逻辑并发（连接池外再加一道闸）
_STATUS_SEM_LIMIT = 15
_status_sem: asyncio.Semaphore | None = None
_status_pool_stats = {
    "in_flight": 0,          # 正在 status_httpx_client 上下文中的协程数（≠连接占用）
    "peak_in_flight": 0,
    "acquire_ok": 0,
    "pool_timeout": 0,
    "http_error": 0,
    "last_ok_ts": 0.0,
    "last_timeout_ts": 0.0,
    "last_timeout_label": "",
}


def _get_status_sem() -> asyncio.Semaphore:
    global _status_sem
    if _status_sem is None:
        _status_sem = asyncio.Semaphore(_STATUS_SEM_LIMIT)
    return _status_sem


def status_pool_stats() -> dict:
    """状态连接池可观测指标（供 /status 使用）。"""
    s = dict(_status_pool_stats)
    s["max_connections"] = _STATUS_POOL_MAX
    s["semaphore_limit"] = _STATUS_SEM_LIMIT
    s["pooled_clients"] = sum(1 for c in _status_clients.values() if c is not None and not c.is_closed)
    s["blocked"] = s["pool_timeout"] > 0
    s["utilization"] = round(s["in_flight"] / max(1, _STATUS_SEM_LIMIT), 2)
    return s


def set_status_only_mode(on: bool | None) -> None:
    """None=按磁盘标记探测；True=扫描期间只保状态推送；False=关闭。"""
    global _STATUS_POOL_FLAG
    _STATUS_POOL_FLAG = on
    try:
        path = os.path.join(os.environ.get("SSM_DATA_DIR", ""), "status_only_mode")
        # 仅当显式 True/False 时写文件，供外部爬虫与插件共用
        if on is None:
            return
        if on:
            open(path, "w").write("1")
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


def status_only_mode_enabled(data_dir: str = "") -> bool:
    if _STATUS_POOL_FLAG is not None:
        return bool(_STATUS_POOL_FLAG)
    base = data_dir or os.environ.get("SSM_DATA_DIR", "")
    if not base:
        return False
    path = os.path.join(base, "status_only_mode")
    try:
        return os.path.exists(path)
    except Exception:
        return False


async def get_status_httpx_client(proxy=None) -> httpx.AsyncClient:
    """状态推送专用 AsyncClient（独立池，不与商店/查价抢连接）。"""
    key = proxy or ""
    client = _status_clients.get(key)
    if client is not None and not client.is_closed:
        return client
    kwargs = httpx_client_kwargs(proxy)
    # 状态池显式指定代理（含直连）；禁止 httpx 再读 HTTP(S)_PROXY 环境变量
    kwargs["trust_env"] = False
    limits = httpx.Limits(
        max_connections=_STATUS_POOL_MAX,
        max_keepalive_connections=max(8, _STATUS_POOL_MAX // 2),
        keepalive_expiry=60.0,
    )
    timeout = httpx.Timeout(20.0, connect=8.0)
    client = httpx.AsyncClient(timeout=timeout, limits=limits, **kwargs)
    old = _status_clients.get(key)
    _status_clients[key] = client
    if old is not None and not old.is_closed:
        _zombie_clients.append(old)
    return client


@asynccontextmanager
async def status_httpx_client(proxy=None, timeout=20.0, follow_redirects=False):
    """上下线/状态轮询专用请求入口。

    注意：in_flight 统计的是「进入请求的协程数」，不是 httpx 连接占用；
    额外用 Semaphore 限制逻辑并发，避免批量失败时打爆状态池。
    """
    sem = _get_status_sem()
    entered = False
    try:
        await sem.acquire()
    except Exception:
        pass
    try:
        raw = await get_status_httpx_client(proxy)
        _status_pool_stats["in_flight"] += 1
        entered = True
        if _status_pool_stats["in_flight"] > _status_pool_stats["peak_in_flight"]:
            _status_pool_stats["peak_in_flight"] = _status_pool_stats["in_flight"]
        _status_pool_stats["acquire_ok"] += 1
        _status_pool_stats["last_ok_ts"] = time.time()
    except (httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout):
        _status_pool_stats["pool_timeout"] += 1
        _status_pool_stats["last_timeout_ts"] = time.time()
        _status_pool_stats["last_timeout_label"] = "acquire"
        clients = list(_status_clients.values())
        _status_clients.clear()
        for c in clients:
            try:
                if c is not None and not c.is_closed:
                    await c.aclose()
            except Exception:
                pass
        try:
            raw = await get_status_httpx_client(proxy)
            _status_pool_stats["in_flight"] += 1
            entered = True
            _status_pool_stats["acquire_ok"] += 1
            _status_pool_stats["last_ok_ts"] = time.time()
        except (httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout):
            _status_pool_stats["pool_timeout"] += 1
            _status_pool_stats["last_timeout_ts"] = time.time()
            raise
    view = _SharedClientView(raw, timeout=timeout, follow_redirects=follow_redirects)
    try:
        yield view
    except (httpx.PoolTimeout, httpx.ConnectTimeout, httpx.ReadTimeout):
        _status_pool_stats["pool_timeout"] += 1
        _status_pool_stats["last_timeout_ts"] = time.time()
        _status_pool_stats["last_timeout_label"] = "request"
        # 超时后丢弃状态池，下一次请求重建，避免死连接占满
        clients = list(_status_clients.values())
        _status_clients.clear()
        for c in clients:
            try:
                if c is not None and not c.is_closed:
                    await c.aclose()
            except Exception:
                pass
        raise
    except Exception:
        _status_pool_stats["http_error"] += 1
        raise
    finally:
        if entered:
            _status_pool_stats["in_flight"] = max(0, _status_pool_stats["in_flight"] - 1)
        try:
            sem.release()
        except Exception:
            pass


async def get_shared_httpx_client(proxy=None) -> httpx.AsyncClient:
    """按代理维度复用同一 AsyncClient（keep-alive 连接池）。"""
    key = proxy or ""
    client = _shared_clients.get(key)
    if client is not None and not client.is_closed:
        return client
    kwargs = httpx_client_kwargs(proxy)
    # 池要够大：爬虫/查价/状态探测会并发抢连接；过小会 PoolTimeout
    limits = httpx.Limits(
        max_connections=80,
        max_keepalive_connections=40,
        keepalive_expiry=60.0,
    )
    timeout = httpx.Timeout(20.0, connect=8.0)
    client = httpx.AsyncClient(timeout=timeout, limits=limits, **kwargs)
    old = _shared_clients.get(key)
    _shared_clients[key] = client
    if old is not None and not old.is_closed:
        _zombie_clients.append(old)
    return client


async def reset_shared_httpx_clients():
    """丢弃当前池，强制下次重建（用于 PoolTimeout 后自救）。"""
    old = list(_shared_clients.values())
    _shared_clients.clear()
    for c in old:
        if c is not None and not c.is_closed:
            _zombie_clients.append(c)
            try:
                await c.aclose()
            except Exception:
                pass


@asynccontextmanager
async def shared_httpx_client(proxy=None, timeout=15.0, follow_redirects=False):
    """替代 `async with httpx.AsyncClient(...)`：出块时不关闭底层连接。

    调用方仍可写 `client.get(url)`；timeout/follow_redirects 按原调用注入。
    """
    try:
        raw = await get_shared_httpx_client(proxy)
    except httpx.PoolTimeout:
        await reset_shared_httpx_clients()
        raw = await get_shared_httpx_client(proxy)
    yield _SharedClientView(raw, timeout=timeout, follow_redirects=follow_redirects)


async def shared_httpx_view(proxy=None, timeout=15.0, follow_redirects=False) -> _SharedClientView:
    """获取池化客户端视图（非 async with 场景）；勿对视图调用 aclose。"""
    raw = await get_shared_httpx_client(proxy)
    return _SharedClientView(raw, timeout=timeout, follow_redirects=follow_redirects)


async def aclose_shared_httpx_clients():
    """插件卸载时关闭连接池（含状态专用池）。"""
    clients = list(_shared_clients.values()) + list(_status_clients.values()) + list(_zombie_clients)
    _shared_clients.clear()
    _status_clients.clear()
    _zombie_clients.clear()
    for client in clients:
        try:
            if not client.is_closed:
                await client.aclose()
        except Exception:
            pass


# 已失效/证书主机名不匹配的 Steam CDN，统一改写
_DEAD_STEAM_HOSTS = (
    "media.steamstatic.com",
    "steamcdn-a.akamaihd.net",
    "steamcommunity-a.akamaihd.net",
)
_GOOD_STEAM_HOSTS = (
    "cdn.akamai.steamstatic.com",
    "cdn.cloudflare.steamstatic.com",
    "shared.akamai.steamstatic.com",
)


def rewrite_steam_cdn_url(url: str) -> str:
    """把失效 Steam CDN 主机名改成当前可用的 akamai。"""
    text = str(url or "")
    if not text:
        return text
    for dead in _DEAD_STEAM_HOSTS:
        if dead in text:
            return text.replace(dead, _GOOD_STEAM_HOSTS[0])
    return text


def steam_cdn_candidates(url: str) -> list:
    """返回可用主机名候选列表（含改写后的）。"""
    base = rewrite_steam_cdn_url(url)
    out = []
    if base:
        out.append(base)
    for host in _GOOD_STEAM_HOSTS[1:]:
        alt = rewrite_steam_cdn_url(str(url or ""))
        # 换 host
        if "cdn.akamai.steamstatic.com" in alt:
            alt2 = alt.replace("cdn.akamai.steamstatic.com", host)
            if alt2 not in out:
                out.append(alt2)
    return out


def aiohttp_connector():
    import aiohttp

    return aiohttp.TCPConnector(ssl=_ssl_context) if _ssl_context is not None else None


def requests_verify():
    return _ssl_ca_file or True
