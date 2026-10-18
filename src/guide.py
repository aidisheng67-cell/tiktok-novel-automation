"""把引导词整块渲染成透明 PNG。

需求（仿样板）：多色文字（关键词、App 名为黄色，其余白色）+ 厚黑描边、无背景框，
并内嵌一个软件图标（按 App 名从 input/tubiao/ 调取，可更换）。
drawtext 无法做多色和内嵌图片，故用 Pillow 预渲染成 PNG，再由 FFmpeg 叠加。
"""
from __future__ import annotations

import re
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ICON_EXTS = (".png", ".webp", ".jpg", ".jpeg")
# 中日文标点用的回退字体（Arial 没有「」等字形）
_CJK_FONT = "assets/fonts/msyh.ttc"


def _parse_color(s: str, default=(255, 212, 0)):
    s = (s or "").strip().lstrip("#")
    try:
        if len(s) == 6:
            return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16), 255)
    except ValueError:
        pass
    return (*default, 255)


def find_icon(icon_dir: Path, app_name: str) -> Path | None:
    """按 App 名（不区分大小写、忽略扩展名）找图标。"""
    if not icon_dir.exists():
        return None
    for ext in ICON_EXTS:
        p = icon_dir / f"{app_name}{ext}"
        if p.exists():
            return p
    target = app_name.strip().lower()
    for p in sorted(icon_dir.iterdir()):
        if p.is_file() and p.suffix.lower() in ICON_EXTS \
                and p.stem.strip().lower() == target:
            return p
    return None


def _tokenize(template: str, keyword: str, app: str):
    """把模板按行、按占位符拆成片段。返回 [[(kind,text,role),...], ...]。

    kind: "text" 或 "icon"；role: plain/kw/app（决定颜色）。
    """
    lines = []
    for line in template.split("\n"):
        segs = []
        for tk in re.split(r"(\{keyword\}|\{app\}|\{icon\})", line):
            if tk == "{keyword}":
                segs.append(("text", keyword, "kw"))
            elif tk == "{app}":
                segs.append(("text", app, "app"))
            elif tk == "{icon}":
                segs.append(("icon", "", "plain"))
            elif tk:
                segs.append(("text", tk, "plain"))
        lines.append(segs)
    return lines


def _load_font(path: Path, size: int):
    return ImageFont.truetype(str(path), size)


def _char_font(ch: str, latin: ImageFont.FreeTypeFont,
               cjk: ImageFont.FreeTypeFont | None):
    """有 CJK 字符且 latin 字体缺字形时用 cjk 字体。"""
    if cjk is not None and ord(ch) > 0x2E00:  # CJK 标点/汉字区起点附近
        return cjk
    return latin


def _draw_run(draw, xy, text, latin, cjk, fill, stroke, stroke_fill):
    """逐字绘制，对 CJK 字符用回退字体；返回绘制后的 x 前进量。"""
    x, y = xy
    for ch in text:
        f = _char_font(ch, latin, cjk)
        draw.text((x, y), ch, font=f, fill=fill,
                  stroke_width=stroke, stroke_fill=stroke_fill)
        x += draw.textlength(ch, font=f)
    return x - xy[0]


def _run_width(draw, text, latin, cjk):
    w = 0.0
    for ch in text:
        w += draw.textlength(ch, font=_char_font(ch, latin, cjk))
    return w


def render_guide_png(cfg, keyword: str, out_path: Path, log=None,
                     app: str | None = None) -> Path | None:
    app = app or cfg.app_name
    template = cfg.guide_template
    fs = int(getattr(cfg, "guide_fontsize", 66))
    stroke = int(getattr(cfg, "guide_outline", 6))
    line_gap = int(fs * float(getattr(cfg, "guide_line_gap", 0.42)))
    icon_scale = float(getattr(cfg, "guide_icon_scale", 1.25))
    white = (255, 255, 255, 255)
    yellow = _parse_color(getattr(cfg, "guide_highlight_color", "#FFD400"))
    black = (0, 0, 0, 255)

    latin = _load_font(cfg.root / cfg.font_file, fs)
    cjk_path = cfg.root / _CJK_FONT
    cjk = _load_font(cjk_path, fs) if cjk_path.exists() else None

    # 图标
    icon_img = None
    icon_path = find_icon(cfg.input_icon_dir, app)
    icon_h = int(fs * icon_scale)
    gap = int(fs * 0.18)
    if icon_path:
        im = Image.open(icon_path).convert("RGBA")
        icon_w = max(1, int(icon_h * im.width / im.height))
        icon_img = im.resize((icon_w, icon_h), Image.LANCZOS)
    elif log:
        log(f"[提示] input/tubiao/ 未找到「{app}」图标，引导词暂不带图标")

    lines = _tokenize(template, keyword, app)

    tmp = Image.new("RGBA", (8, 8))
    md = ImageDraw.Draw(tmp)

    def seg_w(seg):
        kind, text, _ = seg
        if kind == "icon":
            return (icon_img.width + gap * 2) if icon_img else 0
        return _run_width(md, text, latin, cjk)

    line_h = fs + line_gap
    line_widths = [sum(seg_w(s) for s in segs) for segs in lines]
    pad = stroke + 8
    icon_over = max(0, (icon_h - fs) // 2)  # 图标比文字高，上下各多出的量
    W = int(max(line_widths)) + pad * 2
    H = int(len(lines) * line_h) + pad * 2 + icon_over * 2

    canvas = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(canvas)

    max_lw = max(line_widths) if line_widths else 0
    align = str(getattr(cfg, "guide_align", "center")).lower()

    y = pad + icon_over
    for li, segs in enumerate(lines):
        # 对齐：center=每行居中，left=左对齐
        if align == "center":
            x = pad + (max_lw - line_widths[li]) / 2
        else:
            x = pad
        for seg in segs:
            kind, text, role = seg
            if kind == "icon":
                if icon_img:
                    x += gap
                    iy = int(y + fs * 0.5 - icon_img.height * 0.5)
                    canvas.alpha_composite(icon_img, (int(x), iy))
                    x += icon_img.width + gap
                continue
            fill = yellow if role in ("kw", "app") else white
            x += _draw_run(draw, (x, y), text, latin, cjk, fill, stroke, black)
        y += line_h

    canvas.save(out_path)
    if log:
        log(f"  引导词图已生成 {out_path.name}"
            + ("" if icon_img else "（无图标）"))
    return out_path
