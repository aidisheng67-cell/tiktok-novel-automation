"""回填:从飞星拉「发布状态 + 播放/点赞」,更新 state.db。

    python feixing_status.py

之后跑 feishu_sync.py 就能在飞书看到最新状态和数据。可放进定时任务定期回填。
"""
from __future__ import annotations

import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from src import brain           # noqa: E402
import feixing_publish          # noqa: E402

# 飞星子任务 state -> 统一英文状态(与 brain.ACTIVE_SQL 一致)
_SUB = {"success": "published", "publish": "published", "audit": "reviewing",
        "audit_failed": "rejected", "publish_failed": "failed"}

# publish_log 列名 -> 飞星 stat 对象字段名。回填时全量写入。
# views/likes 保留旧列名(=play/digg),供 feishu_sync 等下游继续用。
_STAT_MAP = {
    "views": "play", "likes": "digg",
    "play_rate": "play_rate", "digg_rate": "digg_rate",
    "reach": "reach",
    "comment": "comment", "comment_rate": "comment_rate",
    "share": "share", "share_rate": "share_rate",
    "forward": "forward", "forward_rate": "forward_rate",
    "favorite": "favorite",
    "download": "download", "download_rate": "download_rate",
    "total_time_watched": "total_time_watched",
    "average_time_watched": "average_time_watched",
    "full_video_watched": "full_video_watched",
    "full_video_watched_rate": "full_video_watched_rate",
}


def _map_status(task: dict) -> str:
    medias = task.get("medias") or []
    if medias:
        s = _SUB.get(medias[0].get("state"))
        if s:
            return s
    state, audit = task.get("state") or "", task.get("audit_state") or ""
    if state == "publish_success" and audit == "success":
        return "published"
    if audit == "auditing" or state in ("publishing", "wait"):
        return "reviewing"
    if "fail" in audit:                 # 审核失败 = TikTok 拒
        return "rejected"
    if "fail" in state:                 # 发布失败 = 飞星侧没发出去
        return "failed"
    return "reviewing"


def update_status(cfg) -> int:
    con = brain.connect()
    tids = [r["task_id"] for r in con.execute(
        "SELECT DISTINCT task_id FROM publish_log WHERE task_id IS NOT NULL").fetchall()]
    con.close()
    n = 0
    for i in range(0, len(tids), 20):
        batch = tids[i:i + 20]
        d = feixing_publish.api_json(cfg, "/media/task/list", {
            "page": 1, "page_size": 50, "team_id": cfg["team_id"],
            "task_ids": batch, "pub_time_filter": -1, "pub_video_filter": "team"})
        con = brain.connect()
        for t in d.get("list", []):
            con.execute("UPDATE publish_log SET status=? WHERE task_id=?",
                        (_map_status(t), str(t.get("task_id"))))
            n += 1
        con.commit(); con.close()
    return n


def update_stats(cfg) -> int:
    con = brain.connect()
    tids = [r["task_id"] for r in con.execute(
        "SELECT DISTINCT task_id FROM publish_log WHERE task_id IS NOT NULL").fetchall()]
    con.close()
    n = 0
    for i in range(0, len(tids), 50):
        batch = tids[i:i + 50]
        d = feixing_publish.api_json(cfg, "/stat/detail", {
            "team_id": cfg["team_id"], "time_filter": "unlimited",
            "pub_time_filter": -1, "pub_video_filter": 1, "task_ids": batch,
            "media_types": ["tiktok_business"], "groups": [3],
            "page": 1, "page_size": 100})
        stats = d.get("Stats") or d.get("stats") or d.get("list") or []
        if isinstance(stats, dict):
            stats = [stats]
        con = brain.connect()
        cols = list(_STAT_MAP.keys())
        set_clause = ", ".join(f"{c}=?" for c in cols) + ", stat_updated_at=?"
        now = brain._now()
        for g in stats:
            tid = str(g.get("task_id") or "")
            st = g.get("stat") or {}
            if tid:
                vals = [st.get(src) if st.get(src) is not None else 0
                        for src in _STAT_MAP.values()]
                con.execute(f"UPDATE publish_log SET {set_clause} WHERE task_id=?",
                            (*vals, now, tid))
                n += 1
        con.commit(); con.close()
    return n


def main():
    cfg = feixing_publish.load_cfg()
    print("回填发布状态…")
    print(f"  状态更新 {update_status(cfg)} 条")
    print("回填播放/点赞…")
    try:
        print(f"  数据更新 {update_stats(cfg)} 条")
    except Exception as e:
        print(f"  数据接口暂跳过(视频还没数据很正常): {e}")
    print("完成。跑 feishu_sync.py 可同步到飞书。")


if __name__ == "__main__":
    main()
