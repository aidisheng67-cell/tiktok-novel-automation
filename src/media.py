"""FFmpeg/ffprobe 调用封装。"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe_duration(ffprobe: Path, media: Path) -> float:
    """返回媒体时长（秒）。失败抛异常。"""
    out = subprocess.run(
        [str(ffprobe), "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(media)],
        capture_output=True, text=True, encoding="utf-8",
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe 失败: {media}\n{out.stderr}")
    data = json.loads(out.stdout or "{}")
    dur = data.get("format", {}).get("duration")
    if dur is None:
        raise RuntimeError(f"无法读取时长: {media}")
    return float(dur)


def has_video_stream(ffprobe: Path, media: Path) -> bool:
    out = subprocess.run(
        [str(ffprobe), "-v", "error", "-select_streams", "v",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(media)],
        capture_output=True, text=True, encoding="utf-8",
    )
    return out.returncode == 0 and "video" in (out.stdout or "")


def run_ffmpeg(ffmpeg: Path, args: list[str], cwd: Path, log=None) -> None:
    """在 cwd 下运行 ffmpeg。失败抛异常并附带最后若干行 stderr。"""
    cmd = [str(ffmpeg), "-hide_banner", "-y"] + args
    if log:
        log(f"$ ffmpeg {' '.join(args)}")
    proc = subprocess.run(
        cmd, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        tail = "\n".join((proc.stderr or "").splitlines()[-15:])
        raise RuntimeError(f"ffmpeg 失败 (exit {proc.returncode}):\n{tail}")
