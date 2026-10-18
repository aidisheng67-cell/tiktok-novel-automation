"""发布调度命令行。

    python schedule_cli.py plan              # 只出计划(预览,不上传不发布)—— 默认、安全
    python schedule_cli.py run --limit 1     # 真执行:上传+定时发布,先限 1 条试
    python schedule_cli.py run               # 真执行:发布今天整份计划
    python schedule_cli.py run --now         # 真执行:立即发布(不用定时)

默认 plan 是安全预览。run 才会真的上传+发布,请先用 plan 看清楚。
"""
from __future__ import annotations

import argparse
import random
import sys

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from src import config as config_mod
from src import scheduler
from runlock import SingleInstance


def print_plan(plan):
    if not plan:
        print("今天没有可排的发布(可能:视频池空 / 都在冷却期 / 账号今日已满)。")
        return
    print("=" * 64)
    print(f"  发布计划(共 {len(plan)} 条,时间为美东 ET)")
    print("=" * 64)
    for it in plan:
        print(f"  {it['pub_time']:%m-%d %H:%M} ET  [{it.get('slot','')}]  "
              f"{it['account_name'][:16]:<16} ← {it['video_id']:<13} 「{it['keyword']}」")
    print("-" * 60)
    # 每个账号排了几条 / 配额
    by_acc = {}
    for it in plan:
        by_acc.setdefault(it["account_name"], {"got": 0, "quota": it["quota"], "need": it["need"]})
        by_acc[it["account_name"]]["got"] += 1
    print("各账号:")
    for name, d in by_acc.items():
        warn = "  ⚠️候选不足,没排满" if d["got"] < d["need"] else ""
        print(f"  {name[:22]:<22} 排 {d['got']} 条 / 配额目标 {d['quota']}{warn}")
    print("=" * 60)


def main():
    ap = argparse.ArgumentParser(description="发布调度器")
    ap.add_argument("cmd", nargs="?", default="plan", choices=["plan", "run"])
    ap.add_argument("--limit", type=int, help="run 时最多执行几条(试跑用)")
    ap.add_argument("--now", action="store_true", help="run 时立即发布(默认按 pub_time 定时)")
    ap.add_argument("--per-account", type=int, help="每个账号固定发几条(忽略随机配额)")
    args = ap.parse_args()

    cfg = config_mod.load()
    rng = random.Random()
    plan = scheduler.plan_day(cfg, rng, per_account=args.per_account)

    print_plan(plan)

    if args.cmd == "plan":
        print("\n这是预览。要真的发,用:  python schedule_cli.py run --limit 1  (先试 1 条)")
        return

    # run
    if not plan:
        return
    lock = config_mod.ROOT / "_work" / "pipeline.lock"
    try:
        with SingleInstance(str(lock)):
            _execute(cfg, plan, args)
    except RuntimeError as e:
        print(f"跳过本次发布: {e}")


def _execute(cfg, plan, args):
    n = args.limit if args.limit is not None else len(plan)
    mode = "立即发布" if args.now else "定时发布(pub_time)"
    del_after = bool(getattr(cfg, "delete_after_publish", False))
    print(f"\n⚠️ 即将【真实执行】:上传+{mode},本次最多 {n} 条。")
    print(f"   发布成功后自动删本地成品: {'是' if del_after else '否'}")
    print("   (Ctrl+C 可中止;每条会上传数百MB,较慢)")
    done = scheduler.execute(plan, immediate=args.now, limit=args.limit,
                             delete_after=del_after)
    print(f"\n完成:成功发布 {done} 条。用 brain_cli.py status 查看。")

    if done and bool(getattr(cfg, "auto_feishu_sync", False)):
        try:
            import feishu_sync
            print("同步到飞书多维表格…")
            feishu_sync.auto_sync()
        except Exception as e:
            print(f"[提示] 飞书同步跳过: {e}")


if __name__ == "__main__":
    main()
