"""每天定时自动跑(Windows 任务计划触发 daily.bat)。

流程:发布今日批次(定时到 ET 时段)→ 回填状态/播放数据 → 同步飞书 → 记运行日志。
每步都 try/except,一步失败不影响其余;结尾往飞书「运行日志」表追加一条(成功/失败),
并写本地 logs/daily_YYYYMMDD.log。有失败则退出码非 0(任务计划里可见)。
"""
from __future__ import annotations

import datetime as dt
import random
import sys
import traceback
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from runlock import SingleInstance  # noqa: E402


def main() -> int:
    lock_path = ROOT / "_work" / "pipeline.lock"
    try:
        with SingleInstance(str(lock_path)):
            return _run()
    except RuntimeError as e:
        print(f"跳过本次运行: {e}")
        return 2


def _run() -> int:
    lines: list[str] = []

    def log(m):
        print(m)
        lines.append(str(m))

    dry = "dry" in sys.argv[1:]
    started = dt.datetime.now()
    log(f"===== 每日自动跑{'(dry 演练)' if dry else ''} {started:%Y-%m-%d %H:%M} =====")
    planned = ok = 0
    note = "dry演练" if dry else "ok"

    # 1) 发布今日批次(定时到 ET 时段;受全局上限+冷却约束)
    try:
        from src import config, scheduler
        cfg = config.load()
        cap = int(getattr(cfg, "daily_publish_cap", 11))
        plan = scheduler.plan_day(cfg, random.Random())
        planned = len(plan)
        log(f"今日计划 {planned} 条(全局上限 {cap})")
        if dry:
            for it in plan:
                log(f"  [演练] {it['pub_time']:%m-%d %H:%M} ET  {it['account_name'][:14]:<14} "
                    f"← {it['video_id']} 「{it['keyword']}」")
        elif plan:
            ok = scheduler.execute(
                plan, immediate=False,
                delete_after=bool(getattr(cfg, "delete_after_publish", True)), log=log)
        else:
            log("无可发:额度用完 / 视频池空 / 都在冷却期")
    except Exception as e:
        note = f"发布出错:{e}"
        log(note)
        log(traceback.format_exc())

    fail = 0 if dry else (planned - ok)

    # 2) 回填已发布视频的状态/播放数据
    try:
        import feixing_publish
        import feixing_status
        fcfg = feixing_publish.load_cfg()
        feixing_status.update_status(fcfg)
        try:
            feixing_status.update_stats(fcfg)
        except Exception as e:
            log(f"数据回填跳过:{e}")
    except Exception as e:
        log(f"状态回填出错:{e}")

    # 3) 同步飞书 + 追加运行日志
    try:
        import feishu_sync
        feishu_sync.sync()
        feishu_sync.append_run_log({
            "时间": started.strftime("%Y-%m-%d %H:%M"),
            "计划": planned, "成功": ok, "失败": fail,
            "备注": note if note != "ok" else ("全部成功" if fail == 0 else "有失败"),
        })
    except Exception as e:
        log(f"飞书同步/日志出错:{e}")

    log(f"===== 完成:计划 {planned} / 成功 {ok} / 失败 {fail} =====")

    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / f"daily_{started:%Y%m%d}.log").write_text(
        "\n".join(lines), encoding="utf-8")
    return 0 if (dry or (note == "ok" and fail == 0)) else 1


if __name__ == "__main__":
    sys.exit(main())
