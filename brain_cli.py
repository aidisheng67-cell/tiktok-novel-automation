"""大脑命令行:建库、同步账号、扫描视频入池、查看状态。

    python brain_cli.py init            # 建/更新 state.db
    python brain_cli.py sync-accounts   # 从飞星拉取已授权账号入库
    python brain_cli.py sync-videos      # 扫描成品文件夹,把视频登记进"视频池"
    python brain_cli.py status           # 打印总览(库存/账号/今日发布)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# UTF-8 输出(Windows 重定向到文件时防 GBK 崩)
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import brain
from src import config as config_mod

_SEQ_RE = re.compile(r"^(.+) (\d+)$")


def cmd_init():
    brain.init_db()
    print(f"✅ state.db 已就绪: {brain.DB_PATH}")


def cmd_sync_accounts():
    import feixing_publish
    cfg = feixing_publish.load_cfg()
    data = feixing_publish.api_json(
        cfg, "/media/list", {"page": 1, "page_size": 20, "team_id": cfg["team_id"]})
    lst = data.get("list", [])
    for m in lst:
        brain.upsert_account(
            str(m.get("media_id")),
            m.get("name") or m.get("nickname") or "",
            m.get("media_type") or m.get("platform") or "")
    print(f"✅ 同步 {len(lst)} 个账号入库")
    for m in lst:
        print(f"   {m.get('media_id')}  {m.get('name') or m.get('nickname')}")


def cmd_sync_videos():
    cfg = config_mod.load()
    # 扫描当前输出目录 + 默认 output/(去重)
    dirs = []
    for d in (cfg.output_dir, cfg.root / "output"):
        if d not in dirs:
            dirs.append(d)
    n = 0
    for d in dirs:
        if not d.exists():
            continue
        for mp4 in d.glob("*.mp4"):
            stem = mp4.stem
            m = _SEQ_RE.match(stem)
            if m:
                keyword, seq = m.group(1), int(m.group(2))
            else:
                keyword, seq = stem, 1
            cover = mp4.with_name(f"{stem}_cover.png")
            brain.register_video(
                video_id=stem, keyword=keyword, seq=seq,
                file_path=str(mp4),
                cover_path=str(cover) if cover.exists() else None,
                size_mb=round(mp4.stat().st_size / 1024 / 1024, 1))
            n += 1
    print(f"✅ 扫描登记 {n} 个视频进视频池 (扫描目录: {', '.join(str(x) for x in dirs)})")


def cmd_status():
    r = brain.status_report()
    print("=" * 44)
    print("            大脑状态总览")
    print("=" * 44)
    print(f"关键词          : {r['keywords']}")
    print(f"视频池 总数      : {r['videos_total']}")
    print(f"  ├ 待发(pending): {r['videos_pending']}")
    print(f"  └ 已发/已排     : {r['videos_published']}")
    print(f"启用账号        : {r['accounts_active']}")
    print(f"发布记录 累计    : {r['publish_total']}")
    print("-" * 44)
    print("各账号今日发布 / 配额:")
    if r["accounts_detail"]:
        for a in r["accounts_detail"]:
            print(f"  {a['name'][:22]:<22}  今日 {a['today']}  / 配额 {a['quota']}")
    else:
        print("  (还没同步账号,先跑 sync-accounts)")
    print("=" * 44)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    {
        "init": cmd_init,
        "sync-accounts": cmd_sync_accounts,
        "sync-videos": cmd_sync_videos,
        "status": cmd_status,
    }.get(cmd, lambda: print(f"未知命令: {cmd}\n可用: init / sync-accounts / sync-videos / status"))()


if __name__ == "__main__":
    main()
