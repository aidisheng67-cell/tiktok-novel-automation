"""字幕：用 faster-whisper 把小说音频转成 ASS（带时间轴 + 样式）。

为什么用 ASS 而非 SRT：
SRT 经 libass 渲染时，FontSize/MarginV 走的是脚本默认坐标系（高≈288），
在 1920 高的竖屏上会被放大数倍，导致字幕字号和位置完全不可控。
ASS 里显式写 PlayResX/PlayResY=视频尺寸，字号与边距即为真实像素，定位精确。

稳健性：任何失败（模型下载不了、识别为空、库缺失）都不抛给主流程，
返回 None，调用方据此跳过字幕、照常出片。
"""
from __future__ import annotations

import os
from pathlib import Path

_MODEL_CACHE = {}


def _apply_hf_env(cfg):
    """在导入 faster_whisper 前设好镜像与禁用 xet。

    国内 huggingface.co 大文件走 xet CDN 常卡死；改用 hf-mirror.com 镜像
    并禁用 hf_xet（xet 后端不认镜像），走普通 HTTPS 才能下通。
    """
    ep = getattr(cfg, "hf_endpoint", "") or ""
    if ep:
        os.environ["HF_ENDPOINT"] = ep
    if getattr(cfg, "hf_disable_xet", True):
        os.environ["HF_HUB_DISABLE_XET"] = "1"


def _fmt_ass(seconds: float) -> str:
    """ASS 时间戳 H:MM:SS.cc（厘秒）。"""
    if seconds < 0:
        seconds = 0
    cs = int(round(seconds * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _ass_header(cfg, fontsize: int, outline: int, alignment: int,
                margin_v: int) -> str:
    w, h = cfg.output_width, cfg.output_height
    font = cfg.subtitle_font_name
    # PrimaryColour 白，OutlineColour 黑，BorderStyle=1(描边)，无阴影
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {w}\n"
        f"PlayResY: {h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font},{fontsize},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H00000000,-1,0,0,0,100,100,0,0,1,{outline},0,{alignment},"
        f"80,80,{margin_v},1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, Effect, Text\n"
    )


_STRIP_PUNCT = " ,.!?;:\"'()[]，。、！？；：“”‘’…—-"


def _ass_text(text: str) -> str:
    # 去掉 Whisper 常见的行首多余标点/空格
    text = text.strip().lstrip(",，、.。;；:：").strip()
    return text.replace("\n", "\\N").replace("{", "(").replace("}", ")")


def _clean_word(w: str) -> str:
    """清洗单个词：去首尾空格与标点（保留词内的撇号，如 don't）。"""
    return (w or "").strip().strip(_STRIP_PUNCT).strip()


def _sentence_events(cfg, segments) -> list[str]:
    """整句模式：每段一句，底部居中。"""
    events = []
    for seg in segments:
        text = _ass_text((seg.text or "").strip())
        if not text:
            continue
        events.append(
            # 字段: Layer,Start,End,Style,Name,MarginL,MarginR,Effect,Text
            f"Dialogue: 0,{_fmt_ass(seg.start)},{_fmt_ass(seg.end)},"
            f"Default,,0,0,0,{text}"
        )
    return events


def _word_events(cfg, segments) -> list[str]:
    """逐词模式：每个单词一个事件，大字居中略上，显示到下一个词开始。

    可选“弹入”动画：缩放 82%→100%，约 110ms。
    """
    words = []
    for seg in segments:
        for wd in (getattr(seg, "words", None) or []):
            t = _clean_word(getattr(wd, "word", ""))
            if t:
                words.append((float(wd.start), float(wd.end), t))
    if not words:
        return []

    cx = cfg.output_width // 2
    cy = int(cfg.output_height * float(getattr(cfg, "subtitle_center_y", 0.46)))
    pop = bool(getattr(cfg, "subtitle_pop", True))
    max_hold = 1.2  # 长停顿时单词最多多停这么久，避免一个词挂太久
    n = len(words)
    events = []
    for i, (st, en, t) in enumerate(words):
        end = words[i + 1][0] if i + 1 < n else en  # 显示到下一个词开始
        if end - en > max_hold:
            end = en + max_hold
        if end <= st:
            end = st + 0.15
        tag = f"\\pos({cx},{cy})"
        if pop:
            tag += "\\fscx82\\fscy82\\t(0,110,\\fscx100\\fscy100)"
        events.append(
            f"Dialogue: 0,{_fmt_ass(st)},{_fmt_ass(end)},Default,,0,0,0,"
            f"{{{tag}}}{_ass_text(t)}"
        )
    return events


def _resolve_model_ref(cfg) -> str:
    """本地 models/faster-whisper-<name>/ 若存在则优先用（免下载），否则用名字联网下。"""
    name = cfg.whisper_model
    local = cfg.root / "models" / f"faster-whisper-{name}"
    if (local / "model.bin").exists():
        return str(local)
    return name


def _get_model(cfg):
    ref = _resolve_model_ref(cfg)
    if ref in _MODEL_CACHE:
        return _MODEL_CACHE[ref]
    _apply_hf_env(cfg)
    from faster_whisper import WhisperModel
    model = WhisperModel(ref, device="cpu", compute_type="int8")
    _MODEL_CACHE[ref] = model
    return model


def transcribe_to_ass(cfg, audio: Path, ass_path: Path, log=None) -> Path | None:
    if not cfg.whisper_enabled:
        return None
    try:
        if log:
            log(f"  加载字幕模型 {cfg.whisper_model} …")
        model = _get_model(cfg)
    except Exception as e:
        if log:
            log(f"[警告] Whisper 模型加载失败，跳过字幕: {e}")
        return None

    try:
        mode = getattr(cfg, "subtitle_mode", "word")
        if log:
            log(f"  正在识别字幕（{mode} 模式，音频较长时需几分钟）…")
        prompt = getattr(cfg, "whisper_prompt", "") or None
        segments, _info = model.transcribe(
            str(audio),
            language=cfg.whisper_language or None,
            vad_filter=True,
            beam_size=5,
            initial_prompt=prompt,
            word_timestamps=(mode == "word"),
        )
        # segments 是生成器，物化一次（词级需要读 .words）
        segments = list(segments)

        if mode == "word":
            events = _word_events(cfg, segments)
            header = _ass_header(
                cfg,
                fontsize=int(getattr(cfg, "subtitle_word_fontsize", 96)),
                outline=int(getattr(cfg, "subtitle_word_outline", 5)),
                alignment=5,   # 5 = 屏幕正中（配合 \pos 精确定位）
                margin_v=0,
            )
        else:
            events = _sentence_events(cfg, segments)
            header = _ass_header(
                cfg,
                fontsize=int(getattr(cfg, "subtitle_fontsize", 56)),
                outline=int(getattr(cfg, "subtitle_outline", 3)),
                alignment=2,   # 2 = 底部居中
                margin_v=int(getattr(cfg, "subtitle_margin_v", 230)),
            )

        if not events:
            if log:
                log("[警告] Whisper 未识别到语音/词，跳过字幕")
            return None
        ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")
        if log:
            log(f"字幕生成 {len(events)} 条（{mode}）-> {ass_path.name}")
        return ass_path
    except Exception as e:
        if log:
            log(f"[警告] Whisper 转写失败，跳过字幕: {e}")
        return None
