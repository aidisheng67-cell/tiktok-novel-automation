"""把现有成品批量重压到目标体积区间(默认 config 的 target_size_mb_range)。

    python recompress.py                 # 压 config output_dir 里所有 mp4
    python recompress.py "路径"           # 指定文件夹

只重编码视频(降码率),音频直接复制;去重手段(镜像/裁切/调色/噪点)已烙进画面,
重编码完整保留。压完更新大脑里的体积。已经足够小的(<下限)会跳过。
"""
from __future__ import annotations

import random
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import config as config_mod  # noqa: E402
from src import brain  # noqa: E402


def probe_dur(fp: str, video: Path) -> float:
    out = subprocess.run([fp, "-v", "error", "-show_entries", "format=duration",
                          "-of", "csv=p=0", str(video)],
                         capture_output=True, text=True)
    try:
        return float(out.stdout.strip())
    except ValueError:
        return 0.0


def main():
    cfg = config_mod.load()
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else cfg.output_dir
    rng_range = getattr(cfg, "target_size_mb_range", [110, 160])
    lo, hi = float(rng_range[0]), float(rng_range[1])
    rng = random.Random()
    ff, fp = str(cfg.ffmpeg), str(cfg.ffprobe)

    mp4s = sorted(folder.glob("*.mp4"))
    print(f"待重压: {len(mp4s)} 个,目标 {lo:.0f}-{hi:.0f}MB,目录 {folder}")
    done = skipped = failed = 0
    for i, mp4 in enumerate(mp4s, 1):
        size_mb = mp4.stat().st_size / 1024 / 1024
        if size_mb <= hi:                      # 已经够小,跳过
            skipped += 1
            continue
        dur = probe_dur(fp, mp4)
        if dur <= 0:
            print(f"  [{i}/{len(mp4s)}] {mp4.name} 读不到时长,跳过")
            failed += 1
            continue
        target = rng.uniform(lo, hi)
        vid_k = max(400, int(target * 8192 / dur - 128))
        tmp = mp4.with_name(mp4.stem + ".tmp.mp4")
        args = [ff, "-hide_banner", "-y", "-i", str(mp4),
                "-c:v", "libx264", "-preset", cfg.x264_preset,
                "-b:v", f"{vid_k}k", "-maxrate", f"{int(vid_k * 1.6)}k",
                "-bufsize", f"{vid_k * 2}k", "-pix_fmt", "yuv420p",
                "-c:a", "copy", "-movflags", "+faststart", str(tmp)]
        r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        if r.returncode != 0 or not tmp.exists():
            print(f"  [{i}/{len(mp4s)}] {mp4.name} 重压失败: {r.stderr[-150:]}")
            tmp.unlink(missing_ok=True)
            failed += 1
            continue
        mp4.unlink()
        tmp.rename(mp4)
        new_mb = round(mp4.stat().st_size / 1024 / 1024, 1)
        # 更新大脑体积
        con = brain.connect()
        con.execute("UPDATE videos SET size_mb=? WHERE video_id=?", (new_mb, mp4.stem))
        con.commit()
        con.close()
        print(f"  [{i}/{len(mp4s)}] {mp4.name}  {size_mb:.0f}MB -> {new_mb:.0f}MB")
        done += 1
    print(f"完成:重压 {done}  跳过 {skipped}  失败 {failed}")


if __name__ == "__main__":
    main()
