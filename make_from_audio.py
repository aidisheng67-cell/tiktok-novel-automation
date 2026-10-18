"""改文配音 -> 成品视频。每个关键词出「启用账号数」条视频(每号发1条,避免冷却浪费)。

    python make_from_audio.py
    python make_from_audio.py --per-keyword N   # 覆盖:每关键词出 N 条

读 output/audio/*.mp3(文件名 关键词__app__序号),按关键词分组;每个关键词一共出
N=启用账号数 条视频,平均分到该关键词的几条音频上(每条音频≥1)。用对应 app 引导词、
该关键词专用素材夹。登记进池,音频归档 done/。完成后自动同步飞书。
"""
from __future__ import annotations

import json
import random
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import brain            # noqa: E402
from src import config as config_mod  # noqa: E402
from src import render           # noqa: E402

AUDIO_DIR = ROOT / "output" / "audio"
DONE_DIR = AUDIO_DIR / "done"
META_DIR = ROOT / "output" / "scripts" / "meta"   # 改文 agent 的钩子/文案 sidecar


def load_meta(stem: str) -> dict:
    """读取该脚本(=音频同名)的 agent 元数据:hook_type/genre/caption。没有则空。"""
    p = META_DIR / f"{stem}.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def parse_name(stem: str):
    """'C66177__Good Novel__1' -> (keyword, app)。"""
    parts = stem.split("__")
    kw = parts[0].strip()
    app = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None
    return kw, app


def videos_per_keyword(cfg) -> int:
    """每个关键词出多少条视频 = 启用 TikTok 账号数(每号发1条,正好用满、不撞冷却浪费)。"""
    con = brain.connect()
    n = con.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    con.close()
    return max(1, n)


def main():
    cfg = config_mod.load()
    brain.init_db()
    audios = sorted(AUDIO_DIR.glob("*.mp3"))
    if not audios:
        print("output/audio/ 没有待处理音频")
        return
    DONE_DIR.mkdir(parents=True, exist_ok=True)

    N = videos_per_keyword(cfg)
    if "--per-keyword" in sys.argv:
        N = int(sys.argv[sys.argv.index("--per-keyword") + 1])

    groups: dict[str, list[Path]] = defaultdict(list)
    for a in audios:
        groups[parse_name(a.stem)[0]].append(a)
    print(f"每关键词出 {N} 条视频(=启用账号数),共 {len(groups)} 个关键词")

    rng = random.Random()
    ok = 0
    for kw, kw_audios in groups.items():
        app = parse_name(kw_audios[0].stem)[1]
        bg_dir = brain.get_keyword_bg_dir(kw)
        base, extra = divmod(N, len(kw_audios))     # N 条平均分到该关键词的音频上
        for i, a in enumerate(kw_audios):
            m = base + (1 if i < extra else 0)
            if m < 1:                                # 该关键词音频过多,多余的不出片,归档
                shutil.move(str(a), str(DONE_DIR / a.name))
                continue
            print(f"出片 {a.name}  kw={kw} app={app or cfg.app_name} 出{m}条"
                  + (f"  素材={bg_dir}" if bg_dir else ""))
            meta = load_meta(a.stem)
            try:
                results = render.process_audio(cfg, a, print, rng=rng, variants=m,
                                               keyword=kw, app=app, bg_dir=bg_dir)
                for res in results:
                    if res.get("ok") and res.get("output"):
                        out = Path(res["output"])
                        mt = re.match(r"^(.+) (\d+)$", out.stem)
                        seq = int(mt.group(2)) if mt else 1
                        brain.register_video(
                            video_id=out.stem, keyword=kw, seq=seq, app=app,
                            file_path=str(out), cover_path=res.get("cover"),
                            size_mb=round(out.stat().st_size / 1024 / 1024, 1),
                            hook_type=meta.get("hook_type"), genre=meta.get("genre"),
                            caption_text=meta.get("caption"))
                        ok += 1
                shutil.move(str(a), str(DONE_DIR / a.name))
                (META_DIR / f"{a.stem}.json").unlink(missing_ok=True)  # meta 已消费
            except Exception as e:
                print(f"  ✗ {a.name} 失败: {e}")
    print(f"完成:{ok} 条视频")

    if ok and bool(getattr(cfg, "auto_feishu_sync", False)):
        try:
            import feishu_sync
            print("同步到飞书…")
            feishu_sync.auto_sync(print)
        except Exception as e:
            print(f"[提示] 飞书同步跳过: {e}")


if __name__ == "__main__":
    main()
