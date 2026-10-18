"""趋势素材定期采集 —— 自动到期提醒 + 自动开页 + 自动建条目，人工只负责"看和粘"。

    python trend_intake.py status     # 看上次采集时间、是否到期
    python trend_intake.py open       # 立刻采集:开页 + 建当期条目
    python trend_intake.py check      # 到期才动作(给定时任务用，没到期就静默退出)

为什么不自动抓取:Creative Center 有用数据在登录墙后，自动抓取违反 TikTok ToS，
会危及正在运营的账号。上游 20 分钟人工浏览换账号安全，下游全自动。

素材落到 logs/trend_notes.md，供"发现循环"消化成新钩子候选。
"""
from __future__ import annotations

import re
import sys
import webbrowser
import datetime as _dt
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
NOTES = ROOT / "logs" / "trend_notes.md"

INTERVAL_DAYS = 14          # 采集周期:14 天(要改成一个月就设 30)

# 采集时自动打开的页面(已预设 美国 + 近期窗口)
URLS = [
    ("Top Ads (最重要:高CTR创意机制)",
     "https://ads.tiktok.com/business/creativecenter/inspiration/topads/pc/en"),
    ("热门话题 US·近7天",
     "https://ads.tiktok.com/creative/creativeCenter/trends/hashtag?region=US&period=7"),
    ("热门视频 US·近7天",
     "https://ads.tiktok.com/creative/creativeCenter/trends/video?region=US&period=7"),
]

HEADER = """# 趋势素材库 · TikTok Creative Center

人工采集(低频)→ 本文件 → 发现循环消化成新钩子候选 → 进 HOOK_LIBRARY → 用自己的数据验证。

**采集要点**:Top Ads 里筛 `Region=United States` + `Add industry`(选书/娱乐/App)
+ `Objective=App Install`,重点抄每条广告下面那句**创意机制描述**和它的 **CTR 百分位**。

---
"""

TEMPLATE = """
## {date}

**来源筛选**:Creative Center · US · Top Ads · industry=`____` · objective=`App Install` · 近30天

### 高CTR创意机制(重点·直接粘原文)
<!-- 每条一行,格式: - [CTR百分位] 原文描述 -->
- [Top __%]
- [Top __%]
- [Top __%]

### 相关热门话题(只记与小说/剧情/情感相关的,无关的别抄)
- #

### 观察备注(你自己看到的规律,可选)
-

---
"""


def _read() -> str:
    if NOTES.exists():
        return NOTES.read_text(encoding="utf-8")
    return ""


def last_date() -> _dt.date | None:
    """从素材文件里取最后一次采集日期。"""
    dates = re.findall(r"^##\s+(\d{4}-\d{2}-\d{2})", _read(), flags=re.M)
    if not dates:
        return None
    try:
        return max(_dt.date.fromisoformat(d) for d in dates)
    except ValueError:
        return None


def days_since() -> int | None:
    d = last_date()
    return None if d is None else (_dt.date.today() - d).days


def cmd_status() -> None:
    d, n = last_date(), days_since()
    print("=" * 52)
    print("  趋势素材采集 · 状态")
    print("=" * 52)
    print(f"  素材文件  : {NOTES}")
    print(f"  采集周期  : 每 {INTERVAL_DAYS} 天")
    if d is None:
        print("  上次采集  : 从未采集")
        print("  是否到期  : 是 → 跑 `python trend_intake.py open`")
    else:
        print(f"  上次采集  : {d}  ({n} 天前)")
        due = n >= INTERVAL_DAYS
        print(f"  是否到期  : {'是 → 跑 `python trend_intake.py open`' if due else f'否(还差 {INTERVAL_DAYS - n} 天)'}")
    cnt = len(re.findall(r"^##\s+\d{4}-\d{2}-\d{2}", _read(), flags=re.M))
    print(f"  历史条目  : {cnt} 期")


def cmd_open() -> None:
    NOTES.parent.mkdir(exist_ok=True)
    today = _dt.date.today().isoformat()
    body = _read() or HEADER

    if re.search(rf"^##\s+{today}\b", body, flags=re.M):
        print(f"今天({today})的条目已存在，不重复创建。")
    else:
        body += TEMPLATE.format(date=today)
        NOTES.write_text(body, encoding="utf-8")
        print(f"✅ 已创建当期条目: {NOTES}  (## {today})")

    print("\n正在打开采集页面…")
    for name, url in URLS:
        print(f"   · {name}")
        try:
            webbrowser.open(url)
        except Exception as e:
            print(f"     (打开失败,手动访问: {url}  {e})")

    print(f"""
────────────────────────────────────────────────────
接下来(约 2 分钟):
  1. 在 Top Ads 页筛好 US + 行业 + App Install
  2. 抄 3-5 条高 CTR 广告下面那句「创意机制描述」+ 它的 CTR 百分位
  3. 粘进 {NOTES} 里今天那一段
  4. 存盘即可 —— 下游发现循环会自动消化
────────────────────────────────────────────────────""")


def cmd_check() -> None:
    """定时任务入口:到期才动作,没到期静默退出。"""
    n = days_since()
    if n is not None and n < INTERVAL_DAYS:
        print(f"未到期(距上次 {n} 天 / 周期 {INTERVAL_DAYS} 天),跳过。")
        return
    print(f"⏰ 趋势素材采集已到期(距上次 {n if n is not None else '—'} 天)")
    cmd_open()


def main() -> None:
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "status").lower()
    if cmd == "status":
        cmd_status()
    elif cmd == "open":
        cmd_open()
    elif cmd == "check":
        cmd_check()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
