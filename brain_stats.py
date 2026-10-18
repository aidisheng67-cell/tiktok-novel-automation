"""反馈优化大脑 · 统计引擎 v1

在【账号内】比较不同钩子(hook_type)的表现，再看【跨账号共识】——
账号权重被锁定抵消，共识比单账号小样本均值更可靠。

    python brain_stats.py                      # 默认:完播率(没有则回退点赞率), 满7天, 每格>=4
    python brain_stats.py --metric like        # 强制用点赞率
    python brain_stats.py --days 7 --min-n 4   # 成熟窗口/每格最小样本

指标优先级:完播率 full_video_watched_rate(主) > 点赞率 likes/views(回退)。
key 续费并回填后，完播率有值，自动升为主指标。
"""
from __future__ import annotations

import sys
import json
import argparse
import datetime as _dt
from collections import defaultdict

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from src import brain    # noqa: E402
from src import levers   # noqa: E402


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def _stderr(xs):
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    var = sum((x - m) ** 2 for x in xs) / (len(xs) - 1)
    return (var / len(xs)) ** 0.5


def load_rows(days: int, column: str):
    """成熟(created_at 距今>=days)且已发布的记录，联 videos 取 <当前杠杆列> 作为 arm + genre。"""
    if column not in levers.ALLOWED_COLUMNS:
        raise ValueError(f"未知杠杆列: {column}")
    con = brain.connect()
    cutoff = (_dt.datetime.now() - _dt.timedelta(days=days)).isoformat()
    rows = con.execute(f"""
        SELECT p.account_id AS acct,
               COALESCE(NULLIF(v.{column},''),'(untagged)') AS arm,
               COALESCE(NULLIF(v.genre,''),'-')             AS genre,
               p.views AS views, p.likes AS likes,
               p.full_video_watched_rate AS comp,
               p.average_time_watched   AS avgwatch
        FROM publish_log p
        LEFT JOIN videos v ON v.video_id = p.video_id
        WHERE p.status='published' AND COALESCE(p.created_at,'') <= ?
    """, (cutoff,)).fetchall()
    con.close()
    return rows


def pick_metric(rows, forced: str | None):
    """决定用哪个指标。返回 (名称, 取值函数)。取值函数返回 None 表示该行无此指标。"""
    def like(r):
        return (r["likes"] / r["views"]) if (r["views"] or 0) > 0 else None

    def comp(r):
        return r["comp"] if r["comp"] is not None else None

    if forced == "like":
        return "点赞率 likes/views", like
    if forced == "completion":
        return "完播率 full_video_watched_rate", comp
    # auto: 有足够完播率数据就用它，否则回退点赞率
    have_comp = sum(1 for r in rows if r["comp"] is not None)
    if have_comp >= 10:
        return "完播率 full_video_watched_rate", comp
    return "点赞率 likes/views (完播率待 key 续费回填)", like


def _all_arms(rows, genre, lever):
    """某题材下该杠杆的"全臂"列表(用于把未进共识的臂补在赢家之后)。
    hook_type 用 rewrite_agent 的静态偏好顺序;其他杠杆用数据里出现过的臂。"""
    if lever == "hook_type":
        try:
            from rewrite_agent import GENRE_WEIGHTS
            return list(GENRE_WEIGHTS.get(genre, GENRE_WEIGHTS.get("other", [])))
        except Exception:
            pass
    seen = []
    for r in rows:
        a = r["arm"]
        if a != "(untagged)" and a not in seen:
            seen.append(a)
    return seen


def _consensus_for_genre(rows, genre, mfn, min_n):
    """某题材内:账号内比钩子 -> 跨账号净胜。返回(数据排序赢家list, 净胜dict, 样本数)。"""
    cells = defaultdict(list)
    for r in rows:
        if r["genre"] != genre or r["arm"] == "(untagged)":
            continue
        val = mfn(r)
        if val is not None:
            cells[(r["acct"], r["arm"])].append(val)
    hooks = sorted({h for (a, h) in cells})
    accts = sorted({a for (a, h) in cells})
    win = defaultdict(int)
    for i in range(len(hooks)):
        for j in range(i + 1, len(hooks)):
            A, B = hooks[i], hooks[j]
            aw = bw = 0
            for acct in accts:
                va, vb = cells.get((acct, A)), cells.get((acct, B))
                if va and vb and len(va) >= min_n and len(vb) >= min_n:
                    if _mean(va) > _mean(vb):
                        aw += 1
                    elif _mean(vb) > _mean(va):
                        bw += 1
            win[A] += aw - bw
            win[B] += bw - aw
    ranked = [h for h, _ in sorted(win.items(), key=lambda kv: -kv[1])]
    return ranked, dict(win), sum(len(v) for v in cells.values())


def emit_direction(days, min_n, metric):
    """把【当前 active 杠杆】各题材的数据排序写成 logs/direction_<lever>.json。"""
    lever = levers.active_lever()
    column = levers.lever_column(lever)
    if not column:
        print(f"❌ active 杠杆 {lever} 无有效列，检查 levers.json"); return
    rows = load_rows(days, column)
    mname, mfn = pick_metric(rows, metric)
    genres = {r["genre"] for r in rows if r["genre"] not in ("-", "")}
    out = {"generated_at": _dt.datetime.now().isoformat(timespec="seconds"),
           "lever": lever, "column": column,
           "metric": mname, "min_n": min_n, "days": days, "genres": {}}
    for g in sorted(genres):
        ranked, win, ns = _consensus_for_genre(rows, g, mfn, min_n)
        # 需要至少 2 个臂形成了共识(净胜非全 0)才给方向
        if len([h for h in ranked if win.get(h, 0) != 0]) < 2:
            continue
        arms = _all_arms(rows, g, lever)
        full = ranked + [h for h in arms if h not in ranked]  # 赢家在前，其余补后
        out["genres"][g] = {
            "order": full,
            "ranked_by_data": ranked,
            "net_wins": win,
            "samples": ns,
            "confidence": "low",   # 当前小样本，一律 low；换完播率+更多样本再升
        }
    path = levers.direction_path(lever)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"✅ 方向已写入: {path}")
    print(f"   杠杆={lever}(列 {column})  指标={mname}  覆盖题材={list(out['genres'].keys())}")
    for g, d in out["genres"].items():
        print(f"   [{g}] 数据排序: {' > '.join(d['ranked_by_data'])}  (样本{d['samples']}, {d['confidence']})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--metric", choices=["like", "completion"], default=None)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--min-n", type=int, default=4)
    ap.add_argument("--emit", action="store_true", help="把方向写入 logs/direction_<lever>.json")
    args = ap.parse_args()

    if args.emit:
        emit_direction(args.days, args.min_n, args.metric)
        return

    lever = levers.active_lever()
    column = levers.lever_column(lever)
    if not column:
        print(f"❌ active 杠杆 {lever} 无有效列，检查 levers.json"); return
    rows = load_rows(args.days, column)
    metric_name, mfn = pick_metric(rows, args.metric)

    # (账号,臂) -> [指标值...]  仅收有标签、有指标值的
    cells = defaultdict(list)
    hooks_seen = set()
    for r in rows:
        val = mfn(r)
        if val is None or r["arm"] == "(untagged)":
            continue
        cells[(r["acct"], r["arm"])].append(val)
        hooks_seen.add(r["arm"])

    print("=" * 60)
    print("  反馈优化大脑 · 统计引擎 v2 (杠杆框架)")
    print("=" * 60)
    print(f"  当前杠杆  : {lever}  (videos.{column})")
    print(f"  指标      : {metric_name}")
    print(f"  成熟窗口  : 发布满 {args.days} 天")
    print(f"  每格最小n : {args.min_n}")
    print(f"  有标签成熟样本: {sum(len(v) for v in cells.values())} 条, 覆盖 {len(hooks_seen)} 种臂")

    if not cells:
        print("\n  没有可分析的带标签成熟样本。")
        return

    # ---- 1) 各钩子总体均值(方向性,不做显著性) ----
    by_hook = defaultdict(list)
    for (acct, hook), vals in cells.items():
        by_hook[hook].extend(vals)
    print("\n【总体均值 · 方向性参考】(样本少，仅供方向)")
    print(f"  {'钩子':<20}{'均值':>10}{'样本':>8}")
    for hook, vals in sorted(by_hook.items(), key=lambda kv: -_mean(kv[1])):
        print(f"  {hook:<18}{_mean(vals):>10.4f}{len(vals):>8}")

    # ---- 2) 账号内排名(账号权重被锁定) ----
    print("\n【账号内排名】(每账号内比钩子，n≥min-n 才计)")
    accts = sorted({a for (a, h) in cells})
    for acct in accts:
        arms = [(h, cells[(acct, h)]) for (a, h) in cells if a == acct]
        arms = [(h, v) for h, v in dict(arms).items() if len(v) >= args.min_n]
        if len(arms) < 2:
            continue
        arms.sort(key=lambda hv: -_mean(hv[1]))
        line = "  ".join(f"{h}={_mean(v):.3f}(±{_stderr(v):.3f},n{len(v)})" for h, v in arms)
        print(f"  acct {acct}: {line}")

    # ---- 3) 跨账号共识(核心稳健信号) ----
    # 对每对钩子(A,B): 统计有多少账号里 A的均值 > B的均值(两边都够样本)
    print("\n【跨账号共识】(小样本下最稳健的方向信号)")
    hooks = sorted(hooks_seen)
    win = defaultdict(int)  # hook -> 净胜账号数
    detail = []
    for i in range(len(hooks)):
        for j in range(i + 1, len(hooks)):
            A, B = hooks[i], hooks[j]
            a_wins = b_wins = 0
            for acct in accts:
                va, vb = cells.get((acct, A)), cells.get((acct, B))
                if va and vb and len(va) >= args.min_n and len(vb) >= args.min_n:
                    if _mean(va) > _mean(vb):
                        a_wins += 1
                    elif _mean(vb) > _mean(va):
                        b_wins += 1
            if a_wins + b_wins == 0:
                continue
            win[A] += a_wins - b_wins
            win[B] += b_wins - a_wins
            lead = A if a_wins >= b_wins else B
            detail.append(f"  {A} vs {B}: {a_wins}:{b_wins} 账号 → 倾向 {lead if a_wins!=b_wins else '平'}")
    if detail:
        for d in detail:
            print(d)
        print("\n  共识排名(净胜账号数):")
        for hook, sc in sorted(win.items(), key=lambda kv: -kv[1]):
            print(f"    {hook:<18} 净胜 {sc:+d}")
    else:
        print("  暂无任何一对钩子在同一账号内都达到最小样本 → 无法形成共识，继续累积。")

    # ---- 4) 诚实结论 ----
    strong_cells = sum(1 for v in cells.values() if len(v) >= args.min_n)
    print("\n【结论】")
    print(f"  达到最小样本的(账号×钩子)格子: {strong_cells} / {len(cells)}")
    if not detail:
        print("  → 数据量还不足以下任何结论。引擎已就绪，随发布累积会自动亮灯。")
    else:
        print("  → 已有方向性共识(见上)，但样本仍小，作为'倾向'而非'定论'；key 续费换完播率后信号会更干净。")


if __name__ == "__main__":
    main()
