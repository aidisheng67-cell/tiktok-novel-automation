"""背景素材：随机挑选、归一化成竖屏、拼接到目标时长。"""
from __future__ import annotations

import random
from pathlib import Path

from . import media
from .dedup import dedup_video_chain

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts"}


def list_backgrounds(bg_dir: Path) -> list[Path]:
    # 递归扫描，子文件夹里的素材也会被找到
    return sorted(p for p in bg_dir.rglob("*")
                  if p.is_file() and p.suffix.lower() in VIDEO_EXTS)


def select_backgrounds(pool: list[Path], ffprobe: Path, target: float,
                       rng: random.Random) -> list[Path]:
    """随机（可重复）抽取素材直到累计时长 >= target。返回被选片段的有序列表。"""
    if not pool:
        raise RuntimeError("背景素材文件夹为空，无法生成画面")
    # 预读时长（缓存）
    durs = {}
    for p in pool:
        try:
            durs[p] = media.probe_duration(ffprobe, p)
        except Exception:
            durs[p] = 0.0
    usable = [p for p in pool if durs[p] > 0.1]
    if not usable:
        raise RuntimeError("背景素材都无法读取时长/无有效视频")

    chosen: list[Path] = []
    total = 0.0
    guard = 0
    while total < target + 0.5:
        p = rng.choice(usable)
        chosen.append(p)
        total += durs[p]
        guard += 1
        if guard > 5000:  # 安全阀，避免异常死循环
            break
    return chosen


def build_base_script(cfg, clips: list[Path], params: dict) -> str:
    """生成 pass1 的 filter_complex 脚本内容。

    每个输入：归一化到 WxH 竖屏（填满裁切）+ 统一 fps + setpts 归零；
    然后 concat；再接去重滤镜链，输出 [outv]。
    """
    w, h = cfg.output_width, cfg.output_height
    base_fps = cfg.fps
    lines = []
    labels = []
    for i in range(len(clips)):
        lbl = f"v{i}"
        lines.append(
            f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,"
            f"crop={w}:{h},setsar=1,fps={base_fps},format=yuv420p,"
            f"setpts=PTS-STARTPTS[{lbl}]"
        )
        labels.append(f"[{lbl}]")
    concat = "".join(labels) + f"concat=n={len(clips)}:v=1:a=0[cat]"
    lines.append(concat)
    lines.append(f"[cat]{dedup_video_chain(cfg, params)}[outv]")
    return ";\n".join(lines)
