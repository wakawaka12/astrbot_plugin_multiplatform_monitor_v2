import time
import io
from typing import Optional
from ...presentation.renderers.steam_list import render_steam_list_image
from ...presentation.renderers.game_start import get_avatar_frame_url, get_avatar_frame_path
from ...infrastructure.clients.multi import split_platform_sid

# 与 alllist 一致的 personastate -> 状态 映射（0离线,1在线,2忙碌,3离开,4打盹）
_PERSONA_STATUS = {0: 'offline', 1: 'online', 2: 'busy', 3: 'away', 4: 'snooze'}

_STATUS_RANK = {
    "playing": 0,
    "online": 1,
    "busy": 2,
    "away": 3,
    "snooze": 4,
    "offline": 5,
    "error": 6,
}

_PLATFORM_TITLES = {
    "steam": "Steam 玩家状态",
    "psn": "PSN 玩家状态",
    "xbox": "Xbox 玩家状态",
    "nso": "NS 玩家状态",
}


def _platform_of(sid: str) -> str:
    sp = split_platform_sid(str(sid))
    return sp[0] if sp else "steam"


def _sort_user_list(user_list):
    """在玩 > 在线未玩 > 离线/异常"""
    return sorted(user_list, key=lambda u: (_STATUS_RANK.get(u.get("status"), 9), u.get("name") or ""))

async def handle_steam_list(self, event, *, font_path: Optional[str] = None, proxy: str = None, platform: Optional[str] = None, **_kwargs):
    '''列出玩家当前状态；platform=steam|psn|xbox 时只渲染该平台'''
    # 获取分群ID
    group_id = None
    if hasattr(event, 'get_group_id'):
        group_id = str(event.get_group_id())
    elif hasattr(event, 'group_id'):
        group_id = str(event.group_id)
    else:
        group_id = 'default'
    direct_steam_ids = self.group_steam_ids.get(group_id, [])
    push_steam_ids = [
        sid
        for sid, push_groups in (getattr(self, 'push_groups', {}) or {}).items()
        if group_id in {str(target) for target in push_groups}
    ]
    steam_ids = list(dict.fromkeys([*direct_steam_ids, *push_steam_ids]))
    user_list = []
    now = int(time.time())
    # 分发群不参与轮询，游玩开始时间应读取对应主监控群的缓存。
    primary_group_by_sid = {}
    for sid in steam_ids:
        if sid in direct_steam_ids:
            primary_group_by_sid[sid] = group_id
            continue
        primary_group_by_sid[sid] = next(
            (
                owner_group_id
                for owner_group_id, owner_steam_ids in self.group_steam_ids.items()
                if sid in {str(owner_sid) for owner_sid in owner_steam_ids}
            ),
            group_id,
        )
    # 批量查询所有玩家状态，减少API调用次数
    status_map = await self.fetch_player_statuses_batch(steam_ids) if steam_ids else {}
    for sid in steam_ids:
        status = status_map.get(sid)
        if not status:
            user_list.append({
                'sid': sid,
                'name': sid,
                'status': 'error',
                'avatar_url': '',
                'game': '',
                'gameid': '',
                'play_str': '获取失败',
                'lastlogoff': None
            })
            continue
        name = self._resolve_bind_name(sid, status.get('name') or sid)
        gameid = status.get('gameid')
        game = status.get('gameextrainfo')
        lastlogoff = status.get('lastlogoff')
        personastate = status.get('personastate', 0)
        avatar_url = status.get('avatarfull') or status.get('avatar') or ''
        is_multi = bool(split_platform_sid(str(sid)))
        if gameid and not is_multi:
            zh_game_name = await self.get_chinese_game_name(gameid, game) if gameid else (game or "未知游戏")
        else:
            zh_game_name = game or ("未知游戏" if gameid else "")
        if gameid:
            start_time = self.session_service.started_at(
                primary_group_by_sid.get(sid, group_id), sid, gameid
            )
            play_seconds = now - start_time if start_time else 0
            play_minutes = play_seconds / 60
            if play_minutes < 60:
                play_str = f"{play_minutes:.1f}分钟"
            else:
                play_str = f"{play_minutes/60:.1f}小时"
            user_list.append({
                'sid': sid,
                'name': name,
                'status': 'playing',
                'avatar_url': avatar_url,
                'game': zh_game_name,
                'gameid': gameid,
                'play_str': play_str,
                'lastlogoff': lastlogoff
            })
        elif personastate and int(personastate) > 0:
            user_list.append({
                'sid': sid,
                'name': name,
                'status': _PERSONA_STATUS.get(int(personastate), 'online'),
                'avatar_url': avatar_url,
                'game': '',
                'gameid': '',
                'play_str': '',
                'lastlogoff': lastlogoff
            })
        elif lastlogoff:
            hours_ago = (now - int(lastlogoff)) / 3600
            user_list.append({
                'sid': sid,
                'name': name,
                'status': 'offline',
                'avatar_url': avatar_url,
                'game': '',
                'gameid': '',
                'play_str': f"上次在线 {hours_ago:.1f} 小时前",
                'lastlogoff': lastlogoff
            })
        else:
            user_list.append({
                'sid': sid,
                'name': name,
                'status': 'offline',
                'avatar_url': avatar_url,
                'game': '',
                'gameid': '',
                'play_str': '',
                'lastlogoff': lastlogoff
            })
    # 排序：在玩 > 在线 > 离线
    user_list = _sort_user_list(user_list)

    # 只保留指定平台（默认 steam）
    plat = (platform or "steam").lower()
    user_list = [u for u in user_list if _platform_of(u.get("sid", "")) == plat]
    title = _PLATFORM_TITLES.get(plat, "玩家状态")

    if not user_list:
        yield event.plain_result(f"本群没有监控 {title.replace(' 玩家状态', '')} 玩家。")
        return

    # 获取所有用户的头像框（并发；多平台自动跳过）
    from ...presentation.renderers.render_assets import gather_avatar_frames, gather_steam_covers
    avatar_frame_paths = await gather_avatar_frames(
        self.data_dir, [u.get("sid") for u in user_list], proxy=proxy
    )
    # 渲染图片（新版 steam 风格不展示封面；旧版卡片风格需要封面，仅在关闭新风格时预取）
    steam_style = self.config.get('enable_steam_style', False)
    covers = {}
    if not steam_style:
        covers = await gather_steam_covers(self, user_list, proxy=proxy)

    img_bytes = await render_steam_list_image(
        self.data_dir, user_list, font_path=font_path, proxy=proxy,
        avatar_frame_paths=avatar_frame_paths, covers=covers,
        steam_style=steam_style, title=title,
    )
    if img_bytes:
        with io.BytesIO(img_bytes) as buf:
            import tempfile
            with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
                tmp.write(buf.read())
                tmp_path = tmp.name
            yield event.image_result(tmp_path)
    else:
        yield event.plain_result("渲染图片失败")
