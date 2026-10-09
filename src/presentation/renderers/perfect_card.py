"""全成就祝贺卡片渲染（大图，信息全部在图内）。"""
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from ...shared.logging import logger

CARD_W, CARD_H = 1000, 560
GOLD = (255, 198, 66)
WHITE = (255, 255, 255)


def _font(path, size):
    try:
        if path:
            return ImageFont.truetype(path, size)
    except Exception:
        pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def _fit_cover(im: Image.Image, w: int, h: int) -> Image.Image:
    """等比缩放并居中裁剪到 w x h。"""
    iw, ih = im.size
    if iw <= 0 or ih <= 0:
        return Image.new("RGB", (w, h), (24, 24, 30))
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _gradient_overlay(w: int, h: int) -> Image.Image:
    ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    for y in range(h):
        a = int(70 + 165 * (y / max(1, h - 1)))
        d.line([(0, y), (w, y)], fill=(0, 0, 0, a))
    return ov


def _clip_text(text: str, max_chars: int) -> str:
    """超长文本截断（超长群昵称常见）。"""
    text = str(text or "")
    if len(text) > max_chars:
        return text[:max(max_chars - 1, 1)] + "…"
    return text


def _fit_font(d, text: str, font_path: str, max_w: float, start_size: int, min_size: int = 20):
    """自适应字号：文本超宽时逐步缩小，保证不溢出。"""
    size = start_size
    while size > min_size:
        f = _font(font_path, size)
        try:
            if d.textlength(text, font=f) <= max_w:
                return f
        except Exception:
            break
        size -= 2
    return _font(font_path, min_size)


def render_perfect_card(player_name: str, game_name: str, unlocked: int, total: int,
                        bg_path: str = "", regular_font: str = "", bold_font: str = "") -> bytes:
    """渲染全成就祝贺卡片（PNG bytes）。背景缺失时用深色渐变兜底。"""
    if bg_path:
        try:
            base = Image.open(bg_path).convert("RGB")
            base = _fit_cover(base, CARD_W, CARD_H)
        except Exception as e:
            logger.debug(f"[perfect_card] 背景图不可用: {e}")
            base = Image.new("RGB", (CARD_W, CARD_H), (22, 24, 32))
    else:
        base = Image.new("RGB", (CARD_W, CARD_H), (22, 24, 32))
    card = Image.alpha_composite(base.convert("RGBA"), _gradient_overlay(CARD_W, CARD_H)).convert("RGB")
    d = ImageDraw.Draw(card)

    # 外描边（金）
    d.rectangle([6, 6, CARD_W - 7, CARD_H - 7], outline=GOLD, width=3)

    def _center(text, font, y, fill=WHITE):
        w = d.textlength(text, font=font)
        d.text(((CARD_W - w) / 2, y), text, font=font, fill=fill)

    f_title = _font(bold_font, 46)
    f_sub = _font(regular_font, 24)
    f_name = _font(bold_font, 52)
    f_game = _font(regular_font, 32)
    f_bar = _font(bold_font, 26)

    # 顶部标题
    _center("全 成 就 达 成", f_title, 52, GOLD)
    _center("P E R F E C T   G A M E", f_sub, 112, (235, 235, 240))

    # 分隔线
    d.line([(CARD_W / 2 - 210, 152), (CARD_W / 2 + 210, 152)], fill=GOLD, width=2)

    p_name = _clip_text(player_name or "神秘玩家", 30)
    f_name = _fit_font(d, p_name, bold_font, CARD_W - 110, 50, 20)
    _center(p_name, f_name, 196)

    g_name = _clip_text(f"《{game_name}》", 34)
    f_game = _fit_font(d, g_name, regular_font, CARD_W - 110, 32, 18)
    _center(g_name, f_game, 276, (255, 224, 150))

    # 进度条
    bar_w, bar_h = 640, 24
    bx, by = (CARD_W - bar_w) / 2, CARD_H - 152
    d.rounded_rectangle([bx, by, bx + bar_w, by + bar_h], radius=12, fill=(255, 255, 255, 70))
    ratio = 1.0
    try:
        if total and total > 0:
            ratio = max(0.0, min(1.0, unlocked / total))
    except Exception:
        ratio = 1.0
    fill_w = max(bar_h, bar_w * ratio)
    d.rounded_rectangle([bx, by, bx + fill_w, by + bar_h], radius=12, fill=GOLD)

    # 进度文字
    _center(f"成就 {unlocked} / {total}   ·   100%", f_bar, int(by + bar_h + 18), GOLD)
    f_foot = _font(regular_font, 20)
    _center("Steam 全成就 · 100% 完成度", f_foot, CARD_H - 46, (200, 200, 210))
    out = BytesIO()
    card.save(out, format="PNG")
    return out.getvalue()
