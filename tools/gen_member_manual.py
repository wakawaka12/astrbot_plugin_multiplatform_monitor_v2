# -*- coding: utf-8 -*-
"""生成「群友可用指令」PDF 手册（仅 MEMBER 权限指令）。"""
import os
import platform
import sys

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
    PageBreak,
)

OUT = r"D:\监控\astrbot_plugin_multiplatform_monitor_v2\assets\docs\game_member_manual.pdf"

CJK_TTF_CANDIDATES = {
    "Windows": [
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\msyhbd.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\simsun.ttc",
    ],
}


def resolve_cjk_font():
    for p in CJK_TTF_CANDIDATES.get(platform.system(), []):
        if os.path.exists(p):
            try:
                pdfmetrics.registerFont(TTFont("CJK", p))
                return "CJK"
            except Exception:
                continue
    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    return "STSong-Light"


CJK = resolve_cjk_font()

TITLE = ParagraphStyle("t", fontName=CJK, fontSize=20, leading=28, textColor=colors.HexColor("#18243a"), spaceAfter=6)
H1 = ParagraphStyle("h1", fontName=CJK, fontSize=14, leading=20, textColor=colors.HexColor("#2f6fed"), spaceBefore=14, spaceAfter=6)
H2 = ParagraphStyle("h2", fontName=CJK, fontSize=12, leading=18, textColor=colors.HexColor("#18243a"), spaceBefore=10, spaceAfter=4)
BODY = ParagraphStyle("b", fontName=CJK, fontSize=10, leading=15, textColor=colors.HexColor("#2a3548"))
MUTED = ParagraphStyle("m", fontName=CJK, fontSize=9, leading=13, textColor=colors.HexColor("#6b7c93"))
CMD = ParagraphStyle("c", fontName=CJK, fontSize=9.5, leading=14, textColor=colors.HexColor("#0b3d91"), leftIndent=4)
NOTE = ParagraphStyle("n", fontName=CJK, fontSize=9, leading=13, textColor=colors.HexColor("#8a4b00"), backColor=colors.HexColor("#fff7e8"), borderPadding=4)

SECTIONS = [
    (
        "1. 快速开始",
        [
            ("说明", "本手册只收录「群成员」可直接使用的指令。开监控、删玩家、愿望单打折推送开关等管理指令不在本手册内，请联系管理员。"),
            ("快捷入口", "发 /game help 或 /steam help 可看功能速查图；发 /game doc（本指令）可再次获取本 PDF。"),
            ("@ 某人", "多数指令支持 @群友；也可写绑定过的 QQ 号或 SteamID64。"),
            ("指令前缀", "支持 / 或 。开头，例如 /price 与 。price 等价。"),
        ],
    ),
    (
        "2. 查状态（在玩什么）",
        [
            ("/game all", "本群总览：Steam / PSN / Xbox 谁在玩、谁在线（图片卡片）。"),
            ("/game list", "同上，别名。"),
            ("/game who @某人", "查「绑定过」的玩家当前状态（未绑定会提示）。"),
            ("/steamwho @某人", "与上类似，查绑定玩家 Steam 状态。"),
            ("/在干嘛 @某人", "steamwho 的口语化别名。"),
            ("/game steam list", "仅 Steam 在线列表。"),
            ("/game ps list", "仅 PSN 在线列表。"),
            ("/game xbox list", "仅 Xbox 在线列表。"),
            ("例", "/game all  ·  /steamwho @小明  ·  /game who 123456789"),
        ],
    ),
    (
        "3. 查价（游戏值不值得买）",
        [
            ("/price 游戏名", "搜索候选，展示价格区服对比。再回复序号如 1 或 1 2 可连查（约 3 分钟内有效）。"),
            ("/px 游戏名", "快捷查价，直接返回第一条匹配。"),
            ("/game price 游戏名", "与 /price 相同。"),
            ("/steam price 游戏名", "与 /price 相同。"),
            ("/steam 游戏名", "一站式：多区价格 + 史低 + 简介 + 商店链接。"),
            ("/steam game AppID", "按 Steam AppID 查详情卡片。"),
            ("示例", "/price 艾尔登法环  →  回复 1\n/px 黑神话：悟空\n/steam 空之轨迹"),
            ("说明", "中文搜不到时可试英文名或去掉副标题；豪华版/套餐会在结果里标出。史低来自商店与 ITAD，仅供参考。"),
        ],
    ),
    (
        "4. 愿望单",
        [
            ("/game wish @某人", "查看对方「公开」Steam 愿望单：分页卡片 + 游戏封面。"),
            ("/game wish 7656…", "用 SteamID64 直接查。"),
            ("注意", "必须是公开愿望单。公开数据可能少于客户端条数（成人向/下架等可能不出现）。多页时会以「合并转发聊天记录」发送，点开才加载图。Steam 接口偶发风控时，可能改用本地缓存或提示稍后再试。"),
        ],
    ),
    (
        "5. 游戏库 / 成就 / 开黑 / 购游戏",
        [
            ("/game lib @某人", "Steam 游戏库卡片（按总时长，含图标与时长）。"),
            ("/game coop @A @B", "开黑雷达：两人库的交集/可一起玩的游戏。"),
            ("/game ach AppID @某人", "查看该玩家在此游戏的成就列表（全成就渲染）。"),
            ("/game activity", "本群最近购游戏日志（默认约 7 天）。"),
            ("/game activity 30", "最近 30 天。"),
            ("/game activity @某人", "只看某人。"),
            ("/game activity 7 @某人", "天数 + 某人可组合。"),
            ("说明", "购游戏检测依赖 Steam 已购库；隐私库或未监控可能看不到。旧日志若无 appid 可能没有封面。"),
        ],
    ),
    (
        "6. 排行榜与时长",
        [
            ("/rank", "本群今日全平台游戏时长排行。"),
            ("/rank 7", "最近 7 天。可换 1/3/30 等天数。"),
            ("/steam rank", "同 /rank。"),
            ("/steam allrank", "跨群排行（视配置）。"),
            ("/steam allrank 7", "跨群 + 天数。"),
            ("/game time @某人", "单人今日时长卡片。"),
            ("/game time @某人 7", "单人最近 7 天。"),
        ],
    ),
    (
        "7. 绑定与备注",
        [
            ("/mybind", "查「你自己」绑定的账号（Steam/PSN/Xbox）与备注。"),
            ("/game bind", "同 /mybind。"),
            ("/game bind @某人", "查指定 QQ 的绑定与备注。"),
            ("/game bind all", "本群绑定一览：已绑定 / 未绑定（按平台压缩显示）。"),
            ("/game bind 7656…", "反查：这个 SteamID 绑到了哪个 QQ。"),
            ("/game bind psn:xxx", "反查 PSN 在线 ID。"),
            ("/game bind xbox:xxx", "反查 Xbox 代号。"),
            ("/game remark 7656… 新备注", "修改该账号显示备注（清空用「清空」）。"),
            ("/game remark @某人 新备注", "修改该 QQ 的备注，并同步名下账号。"),
            ("/steam remark …", "与 /game remark 相同。"),
            ("备注优先级", "单号备注 > QQ 绑定备注 > Steam 昵称 > 完整 ID。"),
            ("示例", "/game bind all\n/game bind 76561199415792116\n/game remark 76561199415792116 黑化"),
        ],
    ),
    (
        "8. 帮助与其它",
        [
            ("/game help", "功能速查图（PNG）。"),
            ("/steam help", "与上相同。"),
            ("/game doc", "发送本 PDF 手册（群友指令版）。"),
            ("/game 手册", "与 /game doc 相同。"),
            ("/steam config", "查看插件当前配置摘要（只读）。"),
            ("/steam list", "Steam 在线列表（兼容旧指令）。"),
        ],
    ),
    (
        "9. 常见问题",
        [
            ("@ 没反应", "确认对方已在本群监控且已绑定 QQ；管理员可用 /game bind all 查看。"),
            ("查价没有中文名", "换英文名/去掉符号再搜；候选列表回复数字选择。"),
            ("愿望单为空/读不到", "对方愿望单需设为公开；或 Steam 暂时风控，稍后再试。"),
            ("没有封面", "新游戏商店资源路径变更时会自动补全；极少数商店无图。"),
            ("排行榜没有我", "需管理员把你加入监控，且你有实际游玩记录。"),
            ("PSN/Xbox 不同步", "PSN 依赖隐私设置；Xbox 需有效的登录 token（管理员维护）。"),
            ("权限提示无权使用", "开监控、删玩家、折扣推送开关等仅管理员；群友用本手册指令即可。"),
        ],
    ),
    (
        "10. 指令速查表（群友）",
        [
            ("状态", "/game all · /game who @某人 · /steamwho @某人"),
            ("查价", "/price · /px · /steam 游戏名"),
            ("愿望单", "/game wish @某人"),
            ("库/成就/开黑", "/game lib · /game ach · /game coop @A @B"),
            ("购游戏", "/game activity [天数] [@某人]"),
            ("排行", "/rank [天数] · /game time @某人 [天数]"),
            ("绑定", "/mybind · /game bind [@某人|all|7656…]"),
            ("备注", "/game remark <ID> 新备注"),
            ("帮助", "/game help · /game doc"),
        ],
    ),
]


def footer(canv, doc):
    canv.saveState()
    canv.setFont(CJK, 8)
    canv.setFillColor(colors.HexColor("#8a96a8"))
    canv.drawString(18 * mm, 12 * mm, "全平台游戏监控 · 群友指令手册（仅成员可用）")
    canv.drawRightString(A4[0] - 18 * mm, 12 * mm, f"第 {doc.page} 页")
    canv.restoreState()


def build_table(rows):
    data = [[Paragraph(f"<b>{k}</b>", CMD), Paragraph(v.replace("\n", "<br/>"), BODY)] for k, v in rows]
    t = Table(data, colWidths=[52 * mm, 122 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), CJK),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f7ff")),
        ("ROWBACKGROUNDS", (1, 0), (1, -1), [colors.white, colors.HexColor("#fafcff")]),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d5deeb")),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
    ]))
    return t


def main():
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    doc = SimpleDocTemplate(
        OUT,
        pagesize=A4,
        leftMargin=16 * mm,
        rightMargin=16 * mm,
        topMargin=14 * mm,
        bottomMargin=18 * mm,
        title="全平台游戏监控 · 群友指令手册",
        author="steam_status_monitor_V3",
    )
    story = [
        Paragraph("全平台游戏监控 · 群友指令手册", TITLE),
        Paragraph("Steam / PlayStation / Xbox  ·  仅收录群成员可直接使用的指令  ·  v4.5.5-mp1", MUTED),
        Spacer(1, 4 * mm),
        Paragraph("本文档由机器人指令 <b>/game doc</b> 发送。管理指令（开关监控、删玩家、打折推送开关等）请咨询管理员，不在本手册范围。", NOTE),
        Spacer(1, 3 * mm),
    ]
    for title, rows in SECTIONS:
        story.append(Paragraph(title, H1))
        story.append(build_table(rows))
        story.append(Spacer(1, 2 * mm))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("反馈：在群里 @管理员，或说明具体指令与截图。祝开黑愉快。", MUTED))
    doc.build(story, onFirstPage=footer, onLaterPages=footer)
    print("saved", OUT, os.path.getsize(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
