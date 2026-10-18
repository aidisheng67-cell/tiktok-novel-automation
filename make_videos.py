"""TikTok 小说推文视频批量生成 —— 入口。

用法：
    把小说音频放入 input/audio/（文件名 = 关键词）
    把背景视频放入 input/background/
    改 config.json（小说App名、每条音频出几条视频等）
    运行：  python make_videos.py

可选参数：
    --variants N         每条音频生成 N 条（不同背景），覆盖 config
    --only 关键词        只处理某一条
    --dedup-level low|medium|high   覆盖去重强度
    --keep-work          保留中间文件（默认清理）
    --no-subs            本次不生成字幕
"""
from __future__ import annotations

import argparse
import random
import sys
import time
import traceback
from pathlib import Path

from src import config as config_mod
from src import render

# 强制 stdout/stderr 用 UTF-8：否则 Windows 默认 GBK 编码遇到 ▶✓✗ 等符号会崩，
# 尤其是把输出重定向到文件时（控制台有 chcp 65001 时才是 UTF-8）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except Exception:
        pass


def make_logger(log_path: Path):
    fh = log_path.open("w", encoding="utf-8")

    def log(msg: str):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line)
        fh.write(line + "\n")
        fh.flush()

    return log, fh


def main(argv=None):
    ap = argparse.ArgumentParser(description="TikTok 小说推文批量生成")
    ap.add_argument("--variants", type=int, help="每条音频生成几条（不同背景），覆盖 config")
    ap.add_argument("--only", help="只处理关键词匹配的音频")
    ap.add_argument("--dedup-level", choices=["low", "medium", "high"])
    ap.add_argument("--model", help="覆盖 Whisper 模型 (tiny/base/small/medium/large-v3)")
    ap.add_argument("--keep-work", action="store_true", help="保留中间文件")
    ap.add_argument("--no-subs", action="store_true", help="本次不生成字幕")
    args = ap.parse_args(argv)

    cfg = config_mod.load()
    cfg.ensure_dirs()
    if args.dedup_level:
        cfg._d["dedup_level"] = args.dedup_level
    if args.no_subs:
        cfg._d["whisper_enabled"] = False
    if args.model:
        cfg._d["whisper_model"] = args.model

    variants = args.variants if args.variants else int(
        getattr(cfg, "variants_per_audio", 1) or 1)
    variants = max(1, variants)

    ts = time.strftime("%Y%m%d_%H%M%S")
    log, fh = make_logger(cfg.logs_dir / f"run_{ts}.log")

    try:
        problems = cfg.check_env()
        if problems:
            for p in problems:
                log(f"[环境错误] {p}")
            log("请先修好环境再运行。")
            return 2

        audios = render.list_audios(cfg.input_audio_dir)
        if args.only:
            audios = [a for a in audios if args.only in a.stem]
        if not audios:
            log(f"input/audio/ 里没有可处理的音频（--only={args.only!r}）。")
            log(f"支持格式: {', '.join(sorted(render.AUDIO_EXTS))}")
            return 1

        total = len(audios) * variants
        log(f"共 {len(audios)} 条音频 × {variants} 条/条 = {total} 条待生成，"
            f"去重档={cfg.dedup_level}，字幕={'开' if cfg.whisper_enabled else '关'}")
        rng = random.Random()
        ok, fail = [], []
        for audio in audios:
            try:
                results = render.process_audio(cfg, audio, log, rng, variants,
                                               keep_work=args.keep_work)
            except Exception as e:
                # 整条音频级别的失败（如探测时长/背景为空）
                log(f"  ✗ 失败「{audio.stem}」: {e}")
                log(traceback.format_exc())
                fail.append((f"{audio.stem}", str(e)))
                continue
            for r in results:
                if r["ok"]:
                    ok.append(r)
                else:
                    fail.append((f"{r['keyword']} 第{r.get('variant', '?')}条",
                                 r.get("error", "未知错误")))

        log("=" * 40)
        log(f"完成：成功 {len(ok)} / 失败 {len(fail)}")
        for r in ok:
            extra = " +字幕" if r["subtitle"] else " (无字幕)"
            log(f"  ✓ {Path(r['output']).name}{extra}")
        for name, err in fail:
            log(f"  ✗ {name}: {err}")

        if ok and getattr(cfg, "auto_feishu_sync", False):
            try:
                import feishu_sync
                log("同步到飞书多维表格…")
                feishu_sync.auto_sync(log)
            except Exception as e:
                log(f"[提示] 飞书同步跳过: {e}")
        return 0 if not fail else 3
    finally:
        fh.close()


if __name__ == "__main__":
    sys.exit(main())
