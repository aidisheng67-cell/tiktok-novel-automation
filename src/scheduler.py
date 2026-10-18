"""发布调度器。

每天:从视频池挑视频 → 分配给账号(避开30天冷却、同号一天不重复关键词、按配额)
→ 排错峰时间 → (可选)上传并定时发布,回写发布记录。

设计:
- plan_day() 只做"规划",纯查询、不碰网络,产出当天分配方案(谁·发什么·几点)。
- execute() 才真正上传+发布;默认在 CLI 里是 dry-run 预览,显式确认才执行。
- 定时:用飞星 pub_time(毫秒时间戳)让平台按时发,调度器跑一次即可。
"""
from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

from . import brain


def _slot_time(now_local: dt.datetime, hour: int, minute: int, second: int) -> dt.datetime:
    """把 now 所在时区的 今天 hour:minute:second 作为发布时刻;若已过(或太近)排到明天。"""
    cand = now_local.replace(hour=hour, minute=minute, second=second, microsecond=0)
    if cand <= now_local + dt.timedelta(minutes=10):
        cand += dt.timedelta(days=1)
    return cand


def _caption(cfg, video: dict) -> str:
    tpl = getattr(cfg, "default_caption_template",
                  "Search {keyword} on {app}  #novel #booktok #fyp")
    # 若视频已绑定文案(改文阶段产出),优先用它
    if video.get("caption_id"):
        con = brain.connect()
        row = con.execute("SELECT text FROM captions WHERE caption_id=?",
                          (video["caption_id"],)).fetchone()
        con.close()
        if row and row["text"]:
            return row["text"]
    return tpl.format(keyword=video["keyword"],
                      app=video.get("app") or cfg.app_name)


def plan_day(cfg, rng: random.Random | None = None,
             now: dt.datetime | None = None,
             per_account: int | None = None) -> list[dict]:
    """规划当天发布方案(不联网)。返回按时间排序的分配列表。

    per_account:指定则每个账号固定排这么多条(忽略随机配额和今日已发计数),
    仍然遵守 30 天冷却、同号一天不重复关键词、视频不重复分配。
    """
    rng = rng or random.Random()
    tz = ZoneInfo(getattr(cfg, "publish_timezone", "America/New_York"))
    now = now or dt.datetime.now(tz)
    if now.tzinfo is None:
        now = now.replace(tzinfo=tz)
    cooldown = int(getattr(cfg, "cooldown_days", 30))
    slots = [tuple(s) for s in getattr(
        cfg, "publish_slots", [[10, 11], [16, 17], [18, 19], [19, 20], [20, 21]])]

    con = brain.connect()
    accts = [dict(r) for r in con.execute(
        "SELECT * FROM accounts WHERE active=1 ORDER BY name").fetchall()]
    pend = [dict(r) for r in con.execute(
        "SELECT * FROM videos WHERE status='pending' ORDER BY keyword,seq").fetchall()]
    con.close()

    # 全局每日上限(飞星额度):今天全部账号还能发几条,均分给各号。
    # 靠 count_today_all 天然实现幂等——同一天再跑,已发的算进去,剩余为 0 就不再发。
    cap = int(getattr(cfg, "daily_publish_cap", 11))
    remaining = max(0, cap - brain.count_today_all())
    n_accts = len(accts)
    shares = [remaining // n_accts + (1 if i < remaining % n_accts else 0)
              for i in range(n_accts)] if n_accts else []

    assigned_videos: set[str] = set()
    slot_usage: dict[int, int] = {}    # 时段索引 -> 已排入账号数(跨账号同段错峰用)
    plan: list[dict] = []

    for i, a in enumerate(accts):
        if per_account is not None:
            quota = need = int(per_account)            # 固定条数模式
        else:
            quota = int(getattr(cfg, "per_account_videos", 3))   # 每号固定 3 条
            already_today = brain.count_today(a["account_id"])
            need = max(0, quota - already_today)
        need = min(need, len(slots), shares[i])        # 最多 5 条(5 时段)且不超全局额度

        acc_keywords: set[str] = set()
        picks: list[dict] = []
        cand = [v for v in pend if v["video_id"] not in assigned_videos]
        rng.shuffle(cand)
        for v in cand:
            if len(picks) >= need:
                break
            kw = v["keyword"]
            if kw in acc_keywords:                     # 同号一天不重复同关键词
                continue
            if not brain.can_publish(a["account_id"], kw, cooldown):  # 30天冷却
                continue
            picks.append(v)
            acc_keywords.add(kw)
            assigned_videos.add(v["video_id"])

        # 从 5 个时段里给这个号选 n 个(不足 5 条就随机挑),按时间先后排
        n = len(picks)
        chosen = (sorted(rng.sample(range(len(slots)), n))
                  if n < len(slots) else list(range(len(slots))))
        for v, si in zip(picks, chosen):
            k = slot_usage.get(si, 0)                  # 该时段已排了几个账号
            minute = min(57, 3 + k * rng.randint(6, 11) + rng.randint(0, 2))
            slot_usage[si] = k + 1
            t = _slot_time(now, slots[si][0], minute, rng.randint(0, 59))
            plan.append({
                "account_id": a["account_id"], "account_name": a["name"],
                "video_id": v["video_id"], "keyword": v["keyword"],
                "file_path": v["file_path"], "cover_path": v.get("cover_path"),
                "pub_time": t, "slot": f'{slots[si][0]:02d}-{slots[si][1]:02d}',
                "caption": _caption(cfg, v),
                "quota": quota, "need": need, "got": n,
            })

    plan.sort(key=lambda x: x["pub_time"])
    return plan


def _delete_local(item: dict, log) -> None:
    """删除本地成品(视频+封面),并在库里清空路径。"""
    from pathlib import Path
    for key in ("file_path", "cover_path"):
        p = item.get(key)
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as e:
                log(f"    [提示] 删除 {p} 失败: {e}")
    brain.mark_file_deleted(item["video_id"])
    log(f"    🗑 已删本地成品 {item['video_id']}")


def execute(plan: list[dict], immediate: bool = False, limit: int | None = None,
            delete_after: bool = False, log=print) -> int:
    """真正上传+发布。immediate=True 立即发,否则用 pub_time 定时。返回成功数。

    delete_after=True 时,发布成功后删除本地成品(视频已在飞星,省磁盘)。
    """
    import feixing_publish
    from pathlib import Path
    fcfg = feixing_publish.load_cfg()
    done = 0
    for item in plan:
        if limit is not None and done >= limit:
            break
        try:
            log(f"→ 上传 {item['video_id']} …")
            vid = feixing_publish.upload_video(fcfg, Path(item["file_path"]))
            pub_ms = None if immediate else int(item["pub_time"].timestamp() * 1000)
            task_id = feixing_publish.publish_task(
                fcfg, item["account_id"], vid, item["keyword"],
                item["caption"], draft=False, pub_time_ms=pub_ms)
            brain.record_publish(
                item["video_id"], item["account_id"], item["keyword"],
                status=("published" if immediate else "scheduled"),
                scheduled_time=item["pub_time"].isoformat(), task_id=task_id)
            when = "立即" if immediate else item["pub_time"].strftime("%H:%M")
            log(f"  ✅ {item['video_id']} → {item['account_name']}  {when}  task={task_id}")
            if delete_after:
                _delete_local(item, log)
            done += 1
        except Exception as e:
            brain.record_publish(item["video_id"], item["account_id"], item["keyword"],
                                 status="failed", error=str(e))
            log(f"  ✗ {item['video_id']} → {item['account_name']} 失败: {e}")
    return done
