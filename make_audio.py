"""改文脚本 -> 克隆配音 -> 匹配语速+响度归一化 -> output/audio/*.mp3。

    python make_audio.py            # 处理 output/scripts/ 里所有 .txt

一次性批量调 .ttsenv 的 tts_clone(只加载一次模型),再对每条:
atempo 匹配原声语速(带每条 ±3% 微抖动,增加音频去重维度)+ loudnorm 到 -14 LUFS。
音频文件名沿用脚本名(含 关键词__app__序号),供后续出片按 关键词/app 处理。
"""
from __future__ import annotations

import json
import random
import shutil
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
from src import config as config_mod  # noqa: E402

# TTS 环境:优先 .ttsenv311(E盘自装 Python 3.11,定时任务环境可用);
# 老 .ttsenv 依赖 AppData\Roaming 里的 uv Python,定时任务读不到 —— 仅作兜底。
_TTS_CANDIDATES = [ROOT / ".ttsenv311" / "Scripts" / "python.exe",
                   ROOT / ".ttsenv" / "Scripts" / "python.exe"]
TTS_PY = next((p for p in _TTS_CANDIDATES if p.exists()), _TTS_CANDIDATES[0])
REF_WAV = ROOT / "assets" / "voice_ref.wav"
SCRIPTS_DIR = ROOT / "output" / "scripts"
AUDIO_OUT = ROOT / "output" / "audio"


def _loudnorm(cfg, raw: Path, out_mp3: Path, atempo: float):
    af = "loudnorm=I=-14:TP=-1.5:LRA=11"
    if abs(atempo - 1.0) > 1e-3:
        af = f"atempo={atempo}," + af
    r = subprocess.run([str(cfg.ffmpeg), "-hide_banner", "-y", "-i", str(raw),
                        "-af", af, "-c:a", "libmp3lame", "-b:a", "192k", str(out_mp3)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode != 0 or not out_mp3.exists():
        raise RuntimeError(f"响度归一化失败: {r.stderr[-300:]}")


def main():
    if not TTS_PY.exists():
        print(f"找不到 TTS 环境 {TTS_PY}"); sys.exit(1)
    if not REF_WAV.exists():
        print(f"找不到参考声 {REF_WAV}"); sys.exit(1)
    cfg = config_mod.load()
    AUDIO_OUT.mkdir(parents=True, exist_ok=True)

    # 只处理 "关键词__app__序号" 形式的脚本;辅助文件(如 hook_lines.txt)排除
    files = sorted(p for p in SCRIPTS_DIR.glob("*.txt") if "__" in p.stem)
    if not files:
        print(f"{SCRIPTS_DIR} 里没有脚本 .txt"); return

    xtts_sp = float(getattr(cfg, "tts_xtts_speed", 1.0) or 1.0)
    # 1) 批量克隆配音(一次加载模型)
    jobs, raws = [], {}
    for txt in files:
        raw = AUDIO_OUT / f"{txt.stem}.raw.wav"
        raws[txt] = raw
        jobs.append({"text": str(txt), "ref": str(REF_WAV), "out": str(raw),
                     "lang": "en", "speed": xtts_sp})
    manifest = AUDIO_OUT / "_tts_jobs.json"
    manifest.write_text(json.dumps(jobs, ensure_ascii=False), encoding="utf-8")
    print(f"批量克隆配音 {len(jobs)} 条(一次加载模型)…")
    r = subprocess.run([str(TTS_PY), str(ROOT / "tts_clone.py"), "--batch", str(manifest)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    manifest.unlink(missing_ok=True)
    if r.returncode != 0:
        print(f"TTS 批量失败: {r.stderr[-500:] or r.stdout[-500:]}"); return

    # 2) 每条:匹配语速(带微抖动)+ 响度归一化
    base_atempo = float(getattr(cfg, "tts_speed_factor", 1.0) or 1.0)
    rng = random.Random()
    ok = 0
    for txt in files:
        raw = raws[txt]
        out_mp3 = AUDIO_OUT / f"{txt.stem}.mp3"
        if not raw.exists():
            print(f"  ✗ {txt.name}: 配音缺失"); continue
        atempo = round(base_atempo * (1 + rng.uniform(-0.03, 0.03)), 4)  # ±3% 去重抖动
        try:
            _loudnorm(cfg, raw, out_mp3, atempo)
            raw.unlink(missing_ok=True)
            # 消费脚本:归档到 done/,避免下次重复合成
            done = SCRIPTS_DIR / "done"
            done.mkdir(exist_ok=True)
            shutil.move(str(txt), str(done / txt.name))
            print(f"  ✓ {out_mp3.name} (atempo={atempo})")
            ok += 1
        except Exception as e:
            print(f"  ✗ {txt.name}: {e}")
    print(f"完成:{ok}/{len(files)} 条音频 -> {AUDIO_OUT}")


if __name__ == "__main__":
    main()
