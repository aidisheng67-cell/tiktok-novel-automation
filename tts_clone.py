"""在 .ttsenv (Python 3.11 + PyTorch CUDA) 里运行:XTTS-v2 克隆配音。

单条:  tts_clone.py <text_file> <ref_wav> <out_wav> [lang] [speed]
批量:  tts_clone.py --batch <manifest.json>
        manifest = [{"text": "...txt", "ref": "...wav", "out": "...wav",
                     "lang": "en", "speed": 1.2}, ...]

批量模式**只加载一次模型**(省去每条重载 ~1.8GB 的开销)。主环境(3.14)通过子进程调它。
"""
import json
import os
import sys

os.environ["COQUI_TOS_AGREED"] = "1"       # 自动同意 XTTS 许可,免交互卡住


def _load_tts():
    import torch
    # torch>=2.4/2.6 默认 weights_only=True 会拒绝加载 XTTS 检查点(可信官方模型)
    _orig_load = torch.load

    def _patched_load(*a, **k):
        k["weights_only"] = False
        return _orig_load(*a, **k)

    torch.load = _patched_load
    from TTS.api import TTS
    dev = "cuda" if torch.cuda.is_available() else "cpu"
    tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2").to(dev)
    print(f"model loaded device={dev}", flush=True)
    return tts


def _synth(tts, text_file, ref_wav, out_wav, lang="en", speed=1.0):
    text = open(text_file, encoding="utf-8").read().strip()
    tts.tts_to_file(text=text, speaker_wav=ref_wav, language=lang,
                    file_path=out_wav, split_sentences=True, speed=float(speed))


def main():
    if len(sys.argv) < 2:
        print("用法见文件头"); sys.exit(2)

    if sys.argv[1] == "--batch":
        jobs = json.loads(open(sys.argv[2], encoding="utf-8").read())
        tts = _load_tts()
        done = 0
        for j in jobs:
            try:
                _synth(tts, j["text"], j["ref"], j["out"],
                       j.get("lang", "en"), j.get("speed", 1.0))
                print(f"OK -> {j['out']}", flush=True)
                done += 1
            except Exception as e:
                print(f"FAIL {j.get('out')}: {e}", flush=True)
        print(f"BATCH_DONE {done}/{len(jobs)}")
    else:
        text_file, ref_wav, out_wav = sys.argv[1], sys.argv[2], sys.argv[3]
        lang = sys.argv[4] if len(sys.argv) > 4 else "en"
        speed = float(sys.argv[5]) if len(sys.argv) > 5 else 1.0
        tts = _load_tts()
        _synth(tts, text_file, ref_wav, out_wav, lang, speed)
        print(f"OK -> {out_wav}")


if __name__ == "__main__":
    main()
