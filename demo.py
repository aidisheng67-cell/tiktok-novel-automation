"""离线演示：不联网、不调模型、不依赖 ffmpeg，跑通"状态库 → 发布计划 → 去重参数"。

    python demo.py

做的事情：
1. 在 `_demo/state.db` 建一个临时状态库，写入 3 个账号、5 个关键词、15 条待发视频；
2. 用固定的随机种子和固定时间跑 `scheduler.plan_day()`，打印当天排期；
3. 展示每条视频的随机化去重参数与 FFmpeg 滤镜链；
4. 展示杠杆引擎在"探索 / 利用"之间如何分配样本。

真实流水线还需要 API 凭证、ffmpeg、字体和模型，见 README。
"""
from __future__ import annotations

import datetime as dt
import os
import random
import shutil
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent
DEMO_DIR = ROOT / "_demo"
DEMO_DB = DEMO_DIR / "state.db"

# 必须在导入 src.brain 之前设置，模块在导入时读取该变量
os.environ["NOVEL_DB"] = str(DEMO_DB)
sys.path.insert(0, str(ROOT))

from src import brain, credentials, dedup, levers, scheduler  # noqa: E402
from src.config import Config  # noqa: E402

SEED = 42
ACCOUNTS = [
    ("media_demo_a", "demo_us_01", "US"),
    ("media_demo_b", "demo_us_02", "US"),
    ("media_demo_c", "demo_uk_01", "UK"),
]
KEYWORDS = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]


def seed_database() -> None:
    """重建一个干净的演示库（只动 _demo/，不碰任何真实数据）。"""
    if DEMO_DIR.exists():
        shutil.rmtree(DEMO_DIR)
    DEMO_DIR.mkdir(parents=True)
    brain.init_db()
    for account_id, name, platform in ACCOUNTS:
        brain.upsert_account(account_id, name=name, platform=platform,
                             daily_min=2, daily_max=3, active=1)
    seq = 0
    for kw in KEYWORDS:
        for i in range(3):
            seq += 1
            brain.register_video(
                video_id=f"{kw}_{i + 1:02d}",
                keyword=kw,
                seq=i + 1,
                file_path=f"/demo/output/{kw}_{i + 1:02d}.mp4",
                cover_path=f"/demo/output/{kw}_{i + 1:02d}.png",
                duration=95.0 + i,
                size_mb=40.0 + seq,
                app="Good Novel",
                hook_type=("question", "shock", "cliffhanger")[i % 3],
                genre="romance" if seq % 2 else "revenge",
                caption_text=f"Search {kw} on Good Novel  #booktok",
            )


def print_plan(plan: list[dict]) -> None:
    print(f"\n今日发布计划：{len(plan)} 条（时间为美东 ET）")
    print("-" * 78)
    print(f"{'时间':<16}{'账号':<14}{'视频':<12}{'关键词':<12}文案")
    print("-" * 78)
    for item in plan:
        caption = item["caption"].split("#")[0].strip()
        print(f"{item['pub_time']:%m-%d %H:%M} ET   "
              f"{item['account_name']:<14}{item['video_id']:<12}"
              f"{item['keyword']:<12}{caption[:28]}")
    print("-" * 78)
    by_acc: dict[str, int] = {}
    for item in plan:
        by_acc[item["account_name"]] = by_acc.get(item["account_name"], 0) + 1
    for name, n in sorted(by_acc.items()):
        print(f"  {name}: {n} 条")


def main() -> int:
    seed_database()
    cfg = Config({})
    rng = random.Random(SEED)
    tz = ZoneInfo(cfg.publish_timezone)
    now = dt.datetime(2026, 10, 19, 8, 0, tzinfo=tz)

    print("=" * 78)
    print("TikTok 小说推文自动化 · 离线演示")
    print("=" * 78)
    print(f"状态库: {DEMO_DB}")
    print(f"账号 {len(ACCOUNTS)} 个 / 关键词 {len(KEYWORDS)} 个 / 待发视频 "
          f"{len(brain.pending_videos())} 条")

    plan = scheduler.plan_day(cfg, rng, now=now, per_account=2)
    print_plan(plan)

    print("\n去重参数（每条视频独立随机，固定种子可复现）")
    print("-" * 78)
    demo_rng = random.Random(SEED)
    for item in plan[:3]:
        p = dedup.random_params(cfg, demo_rng)
        chain = dedup.dedup_video_chain(cfg, p)
        print(f"{item['video_id']:<12} zoom={p['zoom']} speed={p['speed']} "
              f"crf={p['crf']} fps={p['fps']} mirror={p['mirror']}")
        print(f"  {chain[:110]}…")

    print("\n杠杆引擎：6 条新视频的钩子分配（30% 探索 / 70% 利用）")
    print("-" * 78)
    arms = ["question", "shock", "cliffhanger", "confession", "countdown"]
    picks = levers.choose_arms("hook_type", "romance", 6, arms)
    print("  " + "  ".join(f"{i + 1}:{a}" for i, a in enumerate(picks)))

    print("\n安全：日志/控制台中的密钥会被脱敏")
    print("-" * 78)
    masked = credentials.redact({"api_key": "sk-demo-secret", "model": "deepseek-chat"})
    print(f"  credentials.redact(...) -> {masked}")
    print(f"  credentials.redact_text('...&token=abc123') -> "
          f"{credentials.redact_text('https://example.com/cb?token=abc123')}")

    print("\n演示完成。真实运行请先复制 *.example.json 填入自己的 Key，并准备 ffmpeg / 字体 / 素材。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
