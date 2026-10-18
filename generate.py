"""每日生成:改文 → 克隆配音 → 出片(填充视频池)。

    python generate.py

按顺序跑:rewrite.py(飞书待处理小说→脚本)→ make_audio.py(→配音)→
make_from_audio.py(→视频进池)。供 Windows 任务在发布前触发,或手动跑。
每步独立子进程,一步失败不挡后续(各步只处理当前能处理的)。日志写 logs/generate_*.log。
"""
from __future__ import annotations

import datetime as dt
import subprocess
import sys
from pathlib import Path

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
from runlock import SingleInstance  # noqa: E402

STEPS = ["rewrite.py", "make_audio.py", "make_from_audio.py"]


def main() -> int:
    lock_path = ROOT / "_work" / "pipeline.lock"
    try:
        with SingleInstance(str(lock_path)):
            return _run()
    except RuntimeError as e:
        print(f"跳过本次生成: {e}")
        return 2


def _run() -> int:
    lines: list[str] = []

    def log(m):
        print(m); lines.append(str(m))

    started = dt.datetime.now()
    log(f"===== 每日生成 {started:%Y-%m-%d %H:%M} =====")
    all_ok = True
    for s in STEPS:
        log(f"\n--- {s} ---")
        r = subprocess.run([sys.executable, str(ROOT / s)],
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.stdout:
            log(r.stdout.strip()[-2000:])
        if r.returncode != 0:
            all_ok = False
            log(f"[{s} 退出码 {r.returncode}] {(r.stderr or '')[-500:]}")

    log(f"\n===== 生成结束(用时 {(dt.datetime.now()-started).seconds//60} 分)=====")
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / f"generate_{started:%Y%m%d_%H%M}.log").write_text(
        "\n".join(lines), encoding="utf-8")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main())
