"""文字叠加层：左上角关键词水印、旁白字幕烧录，以及引导词图叠加。

引导词（封面首帧 + 结尾）改为叠加一张预渲染的 PNG（见 guide.py），
以支持多色文字与内嵌软件图标；水印仍用 drawtext。
所有进入 filtergraph 的路径用相对项目根的正斜杠路径（无盘符冒号）。
"""
from __future__ import annotations

from pathlib import Path


def _write_utf8(path: Path, text: str) -> None:
    # 不带 BOM；newline="\n" 禁止 Windows 把 \n 转成 \r\n
    path.write_text(text, encoding="utf-8", newline="\n")


def _rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def prepare_watermark(cfg, task_id: str, keyword: str) -> str:
    """写好左上角水印 textfile，返回相对路径。"""
    wm_f = cfg.work_dir / f"{task_id}_wm.txt"
    _write_utf8(wm_f, keyword)
    return _rel(wm_f, cfg.root)


def build_overlay_filter(cfg, duration: float, wm_rel: str,
                         ass_rel: str | None,
                         guide_input: str | None = None) -> str:
    """构建 pass2 滤镜图，输出 [outv]。

    guide_input：引导词图的输入流标签（如 "2:v"）；None 表示不叠引导词。
    """
    font = cfg.font_file  # 相对路径，已是正斜杠
    # 结尾引导词起始时刻（= 时长 - outro_seconds，短音频不超过一半）
    outro = min(cfg.outro_seconds, max(1.0, duration / 2))
    start = max(0.0, duration - outro)
    parts = []

    # 1) 旁白字幕（若有）
    if ass_rel:
        parts.append(f"ass={ass_rel}")

    # 2) 关键词水印（第 2 帧起）：白字黑描边、无底框，位置可调
    #    有引导词时，结尾那段（引导词出现期间）隐藏水印，避免重复/拥挤
    wm_x = int(getattr(cfg, "watermark_x", 130))
    wm_y = int(getattr(cfg, "watermark_y", 210))
    wm_bw = int(getattr(cfg, "watermark_outline", 4))
    if guide_input is not None:
        wm_enable = f"gte(n,1)*lt(t,{start:.3f})"
    else:
        wm_enable = "gte(n,1)"
    parts.append(
        f"drawtext=fontfile={font}:textfile={wm_rel}"
        f":fontcolor=white:fontsize={cfg.watermark_fontsize}"
        f":borderw={wm_bw}:bordercolor=black@0.9"
        f":x={wm_x}:y={wm_y}:enable='{wm_enable}'"
    )
    chain = ",".join(parts)

    if guide_input is None:
        return f"[0:v]{chain}[outv]"

    # 3) 引导词图：首帧（封面）+ 结尾各显示一次，居中、顶部 guide_top_frac
    top = float(getattr(cfg, "guide_top_frac", 0.30))
    cover = "eq(n,0)+" if cfg.cover_on_first_frame else ""
    enable = f"{cover}gte(t,{start:.3f})"
    return (
        f"[0:v]{chain}[vt];"
        f"[vt][{guide_input}]overlay=(W-w)/2:H*{top}:enable='{enable}'[outv]"
    )
