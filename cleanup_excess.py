"""清理多余视频:每个关键词最多留 N=启用账号数 条(已发/已排/被拒的账号算已占用),
删掉多余的待发视频(它们受 30 天冷却发不出去,是浪费)。锁定中的文件跳过。

    python cleanup_excess.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import brain  # noqa: E402


def main():
    con = brain.connect()
    N = con.execute("SELECT COUNT(*) FROM accounts WHERE active=1").fetchone()[0]
    keywords = [r["keyword"] for r in con.execute(
        "SELECT DISTINCT keyword FROM videos WHERE status='pending'").fetchall()]
    deleted = freed = 0
    for kw in keywords:
        used = con.execute(
            "SELECT COUNT(DISTINCT account_id) FROM publish_log WHERE keyword=? "
            "AND status IN('scheduled','published','reviewing','rejected')",
            (kw,)).fetchone()[0]
        remaining = max(0, N - used)          # 该关键词还能发给几个号 = 还需留几条
        pend = con.execute(
            "SELECT video_id,file_path,cover_path FROM videos WHERE keyword=? "
            "AND status='pending' ORDER BY seq", (kw,)).fetchall()
        excess = pend[remaining:]
        for v in excess:
            locked = False
            for p in (v["file_path"], v["cover_path"]):
                if p and os.path.exists(p):
                    try:
                        freed += os.path.getsize(p)
                        os.remove(p)
                    except PermissionError:
                        locked = True
            if locked:
                continue
            con.execute("UPDATE videos SET status='retired', file_path=NULL, "
                        "cover_path=NULL WHERE video_id=?", (v["video_id"],))
            deleted += 1
        if excess:
            print(f"  {kw}: 已占用 {used} 号,留 {min(remaining, len(pend))} 条待发,"
                  f"删多余 {len(excess)} 条")
    con.commit()
    con.close()
    print(f"共删 {deleted} 条多余视频,释放 {freed/1024/1024/1024:.1f}GB")


if __name__ == "__main__":
    main()
