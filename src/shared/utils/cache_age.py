"""缓存年龄显示格式化。"""


def format_cache_age(seconds) -> str:
    """把秒数格式化成「X 小时 Y 分钟」。"""
    try:
        sec = int(float(seconds or 0))
    except (TypeError, ValueError):
        return "未知"
    if sec < 0:
        return "未知"
    if sec < 60:
        return f"{sec} 秒"
    m = sec // 60
    if m < 60:
        return f"{m} 分钟"
    h = m // 60
    mm = m % 60
    if h < 48:
        return f"{h} 小时 {mm} 分钟" if mm else f"{h} 小时"
    d = h // 24
    hh = h % 24
    return f"{d} 天 {hh} 小时" if hh else f"{d} 天"
