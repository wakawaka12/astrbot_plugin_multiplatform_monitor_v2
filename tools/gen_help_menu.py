# -*- coding: utf-8 -*-
"""
QQ Bot 帮助指令图 — 全平台游戏监控功能速查
风格：浅色玻璃拟态信息海报 / 游戏 UI
比例：16:10 横版 1920x1200
"""
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import os

W, H = 1920, 1200
M = 64

BG_A = (248, 251, 255)
BG_B = (230, 242, 255)

INK = (24, 36, 58)
INK2 = (70, 86, 112)
MUTED = (130, 146, 170)

BLUE = (47, 111, 237)
CYAN = (0, 176, 210)
PURPLE = (128, 82, 232)
RED = (232, 78, 88)
GREEN = (22, 163, 110)
ORANGE = (240, 140, 46)
PINK = (220, 80, 160)

STEAM = (70, 95, 120)
PSN = (40, 90, 220)
XBOX = (16, 140, 70)


def F(sz, bold=False):
    for p in (
        r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
    ):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, sz)
            except Exception:
                pass
    return ImageFont.load_default()


def measure(d, t, f):
    b = d.textbbox((0, 0), t, font=f)
    return b[2] - b[0], b[3] - b[1]


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def bg_gradient():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    for y in range(H):
        c = lerp(BG_A, BG_B, (y / (H - 1)) * 0.75)
        d.line([(0, y), (W, y)], fill=c)
    wash = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    wd = ImageDraw.Draw(wash)
    wd.ellipse((W * 0.45, H * 0.35, W * 1.35, H * 1.25), fill=(168, 140, 255, 70))
    wd.ellipse((-W * 0.2, -H * 0.35, W * 0.45, H * 0.55), fill=(120, 200, 255, 55))
    wash = wash.filter(ImageFilter.GaussianBlur(80))
    return Image.alpha_composite(img.convert("RGBA"), wash).convert("RGB")


def soft_shadow(size, radius=28, alpha=48):
    w, h = size
    sh = Image.new("RGBA", (w + radius * 4, h + radius * 4), (0, 0, 0, 0))
    d = ImageDraw.Draw(sh)
    d.rounded_rectangle(
        (radius * 2, radius * 2, radius * 2 + w, radius * 2 + h),
        radius=radius, fill=(40, 60, 100, alpha),
    )
    return sh.filter(ImageFilter.GaussianBlur(radius))


def glass_card(canvas, box, radius=28, fill=(255, 255, 255, 170), border=(255, 255, 255, 200)):
    x0, y0, x1, y1 = box
    w, h = int(x1 - x0), int(y1 - y0)
    sh = soft_shadow((w, h), radius=24, alpha=36)
    canvas.paste(sh, (int(x0) - 24, int(y0) - 18), sh)
    body = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    bd = ImageDraw.Draw(body)
    bd.rounded_rectangle((0, 0, w - 1, h - 1), radius=radius, fill=fill, outline=border, width=2)
    hl = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(hl).rounded_rectangle((2, 2, w - 3, h // 3), radius=radius, fill=(255, 255, 255, 40))
    body = Image.alpha_composite(body, hl)
    canvas.paste(body, (int(x0), int(y0)), body)


def gradient_orb(canvas, cx, cy, r, c1, c2):
    size = r * 2
    orb = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(orb)
    for i in range(r, 0, -1):
        t = 1 - i / r
        col = lerp(c1, c2, t)
        d.ellipse((r - i, r - i, r + i, r + i), fill=(*col, 255))
    spec = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(spec).ellipse((r * 0.25, r * 0.2, r * 1.1, r * 0.95), fill=(255, 255, 255, 70))
    spec = spec.filter(ImageFilter.GaussianBlur(8))
    orb = Image.alpha_composite(orb, spec)
    canvas.paste(orb, (int(cx - r), int(cy - r)), orb)


def cmd_row(d, x, y, cmd, desc, accent, example=None, compact=False):
    d.ellipse((x, y + 8, x + 8, y + 16), fill=accent)
    d.text((x + 18, y + 2), cmd, font=F(14, True), fill=INK)
    cw, _ = measure(d, cmd, F(14, True))
    if desc:
        d.text((x + 18 + cw + 10, y + 5), desc, font=F(12), fill=MUTED)
    if example:
        d.text((x + 18, y + 24), example, font=F(11), fill=accent)
        return y + (40 if compact else 46)
    return y + 32


def feature_card(layer, d, x0, y0, x1, y1, title, meta, accent, rows, tags=None):
    glass_card(layer, (x0, y0, x1, y1), radius=28, fill=(255, 255, 255, 165))
    d.rounded_rectangle((x0 + 18, y0 + 16, x1 - 18, y0 + 22), radius=3, fill=accent)
    d.text((x0 + 24, y0 + 34), title, font=F(22, True), fill=accent)
    d.text((x0 + 24, y0 + 66), meta, font=F(12), fill=MUTED)
    tx = x0 + 24
    if tags:
        for tag in tags:
            f = F(11, True)
            tw, th = measure(d, tag, f)
            d.rounded_rectangle((tx, y0 + 90, tx + tw + 14, y0 + 90 + 22), radius=11,
                                fill=(*accent, 22), outline=(*accent, 55), width=1)
            d.text((tx + 7, y0 + 93), tag, font=f, fill=accent)
            tx += tw + 22
    ry = y0 + (122 if tags else 96)
    for row in rows:
        cmd, desc = row[0], row[1]
        ex = row[2] if len(row) > 2 else None
        if ry > y1 - 28:
            break
        ry = cmd_row(d, x0 + 24, ry, cmd, desc, accent, example=ex, compact=True)


def main(out):
    img = bg_gradient()
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # HEADER
    gradient_orb(layer, M + 36, 72, 36, (70, 140, 255), (160, 80, 255))
    d.text((M + 20, 52), "游", font=F(26, True), fill=(255, 255, 255))
    d.text((M + 90, 34), "全平台游戏监控", font=F(40, True), fill=INK)
    d.text((M + 90, 86),
           "Steam / PSN / Xbox  ·  状态推送 · 查价 · 愿望单打折 · 绑定备注 · 排行",
           font=F(15), fill=INK2)
    bx = W - M - 240
    d.rounded_rectangle((bx, 36, W - M, 96), radius=18, fill=(47, 111, 237, 28), outline=(47, 111, 237, 90), width=1)
    d.text((bx + 24, 48), "GAME HELP", font=F(12, True), fill=BLUE)
    d.text((bx + 24, 68), "v4.5.5-mp1", font=F(16, True), fill=INK)

    # FLOW
    y0 = 130
    glass_card(layer, (M, y0, W - M, y0 + 92), radius=24, fill=(255, 255, 255, 155))
    steps = [
        ("01", "添加玩家", "/game steam|ps|xbox add …", BLUE),
        ("02", "绑定备注", "ID @QQ 备注 · /game remark", CYAN),
        ("03", "打开监控", "/game on 自动推送", PURPLE),
        ("04", "扩展", "查价·愿望单·打折·排行", ORANGE),
    ]
    sw = (W - 2 * M - 40) // 4
    for i, (n, t, s, c) in enumerate(steps):
        x = M + 20 + i * (sw + 8)
        gradient_orb(layer, x + 18, y0 + 46, 16, c, lerp(c, (255, 255, 255), 0.35))
        d.text((x + 10, y0 + 36), n, font=F(10, True), fill=(255, 255, 255))
        d.text((x + 44, y0 + 22), t, font=F(18, True), fill=INK)
        d.text((x + 44, y0 + 52), s, font=F(12), fill=MUTED)

    # ROW1: platforms
    py = y0 + 108
    ph = 300
    gap = 18
    cw3 = (W - 2 * M - 2 * gap) // 3

    feature_card(layer, d, M, py, M + cw3, py + ph,
                 "Steam 监控", "好友码 / 链接 / SteamID64", STEAM,
                 [
                     ("/game steam add <ID>", "添加"),
                     ("/game steam add <ID> @QQ 备注", "绑定+备注"),
                     ("/game steam del <ID> · list", "删除 / 列表"),
                     ("/game on · off · all", "开关 / 总览"),
                     ("/steamwho @某人", "查绑定状态"),
                 ], ["状态", "成就", "开局卡"])

    feature_card(layer, d, M + cw3 + gap, py, M + 2 * cw3 + gap, py + ph,
                 "PSN / Xbox", "Online ID · Gamertag/XUID", PSN,
                 [
                     ("/game ps add <在线ID> [@QQ]", "PSN 添加"),
                     ("/game xbox add <代号> [@QQ]", "Xbox 添加"),
                     ("/game ps|xbox del / list", "删除 / 列表"),
                     ("Xbox tokens 过期", "需重新 xbox-authenticate"),
                     ("NS 暂停", "上游 API 不可用"),
                 ], ["PSN", "Xbox"])

    feature_card(layer, d, M + 2 * (cw3 + gap), py, W - M, py + ph,
                 "愿望单 · 打折", "本地缓存 6h · 合并转发", PINK,
                 [
                     ("/game wish @某人", "公开愿望单卡片"),
                     ("/game wish_sale on|off", "本群折扣推送"),
                     ("/game wish_sale test @某人", "立即查折扣"),
                     ("/game wish_sale cache|status", "缓存/状态"),
                     ("多款折扣", "按人一条聊天记录"),
                 ], ["愿望单", "打折推送"])

    # ROW2
    ry = py + ph + 20
    rh = 280
    cw4 = (W - 2 * M - 3 * gap) // 4

    feature_card(layer, d, M, ry, M + cw4, ry + rh,
                 "查价", "多区 · 折扣 · 史低", ORANGE,
                 [
                     ("/price <游戏名|链接>", "候选列表"),
                     ("/px <游戏名>", "直接第一条"),
                     ("回 1 / 1 2", "多选连查(3分钟)"),
                     ("/steam <游戏名>", "一站式查价"),
                 ], ["国区等4区"])

    feature_card(layer, d, M + cw4 + gap, ry, M + 2 * cw4 + gap, ry + rh,
                 "绑定 · 备注", "查询 / 反查 / 修改", GREEN,
                 [
                     ("/mybind · /game bind", "查自己绑定"),
                     ("/game bind @某人", "查指定QQ"),
                     ("/game bind all", "本群绑定一览"),
                     ("/game bind 7656xxx", "Steam→QQ 反查"),
                     ("/game remark <ID> 新备注", "改备注 / 清空"),
                 ], ["绑定"])

    feature_card(layer, d, M + 2 * (cw4 + gap), ry, M + 3 * cw4 + gap, ry + rh,
                 "游戏社交", "库 · 开黑 · 成就 · 购游戏", PURPLE,
                 [
                     ("/game lib @某人", "Steam 游戏库"),
                     ("/game coop @A @B", "开黑雷达"),
                     ("/game ach <appid> @某人", "全成就列表"),
                     ("/game activity [天数]", "购游戏日志"),
                     ("购游戏推送", "自动图文卡片"),
                 ], ["卡片渲染"])

    feature_card(layer, d, M + 3 * (cw4 + gap), ry, W - M, ry + rh,
                 "排行 · 其它", "时长榜 · 帮助 · 兼容", BLUE,
                 [
                     ("/rank [天数]", "全平台时长排行"),
                     ("/game time @某人 [天数]", "单人时长卡"),
                     ("/steam rank_on [all|test]", "每日排行推送"),
                     ("/game help · /steam help", "本帮助图"),
                     ("/steam addid 等", "旧指令仍兼容"),
                 ], ["排行"])

    # FOOTER NOTES
    fy = ry + rh + 18
    glass_card(layer, (M, fy, W - M, H - 28), radius=22, fill=(255, 255, 255, 145))
    notes = [
        (RED, "跨平台去重", "同人同游戏约3分钟内只推一张卡"),
        (ORANGE, "愿望单风控", "Steam SSR 偶发403，已用本地缓存+冷却"),
        (GREEN, "折扣推送", "查价精简版：4区价格+封面，按人合并转发"),
        (CYAN, "改备注", "/game remark 也可 清空；改完用 /game bind 核对"),
    ]
    nw = (W - 2 * M - 40) // 4
    for i, (c, k, v) in enumerate(notes):
        x = M + 20 + i * nw
        d.rounded_rectangle((x, fy + 18, x + 4, fy + 48), radius=2, fill=c)
        d.text((x + 16, fy + 16), k, font=F(13, True), fill=INK)
        d.text((x + 16, fy + 36), v, font=F(11), fill=MUTED)

    img = Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    img.save(out, "PNG", optimize=True)
    print("saved", out, img.size)


if __name__ == "__main__":
    main(r"D:\监控\astrbot_plugin_multiplatform_monitor_v2\assets\images\help_menu.png")
