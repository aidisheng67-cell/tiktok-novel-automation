"""杠杆框架 · 串行一次一杠杆的自我进步引擎

原则(见 Claude 记忆 feedback-brain-zidongjianji):
- 一次只【主动试验一个杠杆】(active),其余【冻结在冠军值】(frozen)。
- 只学主效应,不学交互(低样本量下唯一可行)。
- 每个杠杆是一等对象:在 videos 表的哪一列打标、优化哪个指标、状态、探索/利用参数。

注册表 levers.json 是唯一事实来源;方向文件 logs/direction_<lever>.json 由
brain_stats.py --emit 产出。任何生成阶段用 choose_arms() 选臂并把结果打标到视频上。
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REGISTRY = ROOT / "levers.json"
LOG_DIR = ROOT / "logs"

# 只允许这些 videos 列作为杠杆标签列(白名单，防注入 + 防拼错)
ALLOWED_COLUMNS = {"hook_type", "genre", "opening_type", "duration_band",
                   "pacing", "visual_style", "guide_style"}

DEFAULT_REGISTRY = {
    "active_lever": "hook_type",
    "levers": {
        "hook_type": {
            "column": "hook_type", "metric": "auto", "status": "active",
            "champion": None, "min_n": 4, "explore_ratio": 0.30, "exploit_topk": 3,
        },
        # 下一个杠杆(信号干净):开头类型。生成侧支持就位后把 status 改为 active。
        "opening_type": {
            "column": "opening_type", "metric": "completion", "status": "pending",
            "champion": None, "min_n": 4, "explore_ratio": 0.30, "exploit_topk": 3,
        },
    },
}


def load_registry() -> dict:
    if not REGISTRY.exists():
        REGISTRY.write_text(json.dumps(DEFAULT_REGISTRY, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return json.loads(json.dumps(DEFAULT_REGISTRY))
    return json.loads(REGISTRY.read_text(encoding="utf-8"))


def lever_cfg(name: str) -> dict | None:
    return load_registry().get("levers", {}).get(name)


def active_lever() -> str:
    return load_registry().get("active_lever", "hook_type")


def lever_column(name: str) -> str | None:
    cfg = lever_cfg(name) or {}
    col = cfg.get("column")
    return col if col in ALLOWED_COLUMNS else None


def direction_path(lever: str) -> Path:
    return LOG_DIR / f"direction_{lever}.json"


def direction_order(lever: str, genre: str) -> list[str] | None:
    """读方向文件里该题材的数据排序;无则 None。"""
    try:
        d = json.loads(direction_path(lever).read_text(encoding="utf-8"))
        g = d.get("genres", {}).get(genre)
        return g["order"] if g and g.get("order") else None
    except Exception:
        return None


def choose_arms(lever: str, genre: str, n: int, all_arms: list[str]) -> list[str]:
    """为 n 个条目选臂(通用 explore/exploit)。

    - frozen + 有 champion : 全部用冠军值(该杠杆已锁定,不再消耗样本)。
    - 否则                 : 数据方向(无则 all_arms) 做 利用(排名加权前 topk) + 探索(全臂轮询)。
    确定性:不用随机,探索槽均匀铺开,渲染可复现。
    """
    cfg = lever_cfg(lever) or {}
    if cfg.get("status") == "frozen" and cfg.get("champion"):
        return [cfg["champion"]] * n

    order = direction_order(lever, genre) or list(all_arms)
    er = float(cfg.get("explore_ratio", 0.30))
    topk = int(cfg.get("exploit_topk", 3))

    top = order[:topk] or order
    weights = list(range(len(top), 0, -1))                  # 排名线性加权 [K,K-1,...,1]
    inst = sorted(((k + 0.5) / w, h) for h, w in zip(top, weights) for k in range(w))
    exploit_sched = [h for _, h in inst] or list(all_arms)  # 均匀铺开的利用调度

    n_explore = round(n * er)
    explore_at = set()
    if n_explore > 0:
        step = n / n_explore
        explore_at = {int(step * i + step / 2) for i in range(n_explore)}

    out, xi, ei = [], 0, 0
    for i in range(n):
        if i in explore_at:
            out.append(all_arms[ei % len(all_arms)]); ei += 1        # 探索:全臂
        else:
            out.append(exploit_sched[xi % len(exploit_sched)]); xi += 1  # 利用:赢家多拿
    return out
