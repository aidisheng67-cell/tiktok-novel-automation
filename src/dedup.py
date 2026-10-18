"""去重处理：为每条成品随机生成一组处理参数，并拼出 FFmpeg 滤镜链。

参数每条随机化，保证成品之间也互不相同。强度分档 low/medium/high。
所有几何/调色/噪点/帧率变换都在这里；文字层（封面/水印/字幕/引导）在 overlay.py，
且一定在这些变换 **之后** 叠加，因此不会被镜像/裁切影响。
"""
from __future__ import annotations

import random

# 各档强度：数值为“最大偏移/范围”
LEVELS = {
    "low": {
        "zoom": (1.01, 1.03), "bright": 0.02, "contrast": 0.03,
        "sat": 0.05, "hue": 3, "noise": 4,
        "fps_choices": [30], "sharpen": 0.3, "fade": (0.2, 0.4),
    },
    "medium": {
        "zoom": (1.02, 1.06), "bright": 0.03, "contrast": 0.05,
        "sat": 0.08, "hue": 5, "noise": 8,
        "fps_choices": [30, 29, 25], "sharpen": 0.5, "fade": (0.2, 0.6),
    },
    "high": {
        "zoom": (1.04, 1.10), "bright": 0.05, "contrast": 0.08,
        "sat": 0.12, "hue": 8, "noise": 14,
        "fps_choices": [30, 29, 24, 25], "sharpen": 0.8, "fade": (0.3, 0.9),
    },
}


def random_params(cfg, rng: random.Random | None = None) -> dict:
    rng = rng or random
    lv = LEVELS.get(cfg.dedup_level, LEVELS["medium"])
    zoom = round(rng.uniform(*lv["zoom"]), 4)
    sr = getattr(cfg, "dedup_speed_range", None) or [1.0, 1.0]
    return {
        "mirror": bool(cfg.mirror_enabled),
        "zoom": zoom,
        # 裁切偏移比例(0~1)，最终像素在滤镜里按放大后尺寸算
        "crop_fx": round(rng.random(), 4),
        "crop_fy": round(rng.random(), 4),
        "brightness": round(rng.uniform(-lv["bright"], lv["bright"]), 4),
        "contrast": round(rng.uniform(1 - lv["contrast"], 1 + lv["contrast"]), 4),
        "saturation": round(rng.uniform(1 - lv["sat"], 1 + lv["sat"]), 4),
        "hue": round(rng.uniform(-lv["hue"], lv["hue"]), 2),
        "noise": rng.randint(max(1, lv["noise"] // 2), lv["noise"]),
        "fps": rng.choice(lv["fps_choices"]),
        "crf": rng.randint(cfg.final_crf_range[0], cfg.final_crf_range[1]),
        # 新增维度：变速(音视频一起)、锐化/柔化(纹理)、片尾淡出
        "speed": round(rng.uniform(float(sr[0]), float(sr[1])), 4),
        "sharpen": round(rng.uniform(-lv["sharpen"], lv["sharpen"]), 3),
        "fade_out": round(rng.uniform(*lv["fade"]), 2),
    }


def dedup_video_chain(cfg, p: dict) -> str:
    """拼接后的画面去重滤镜链（不含文字层）。作用于已归一化到 WxH 的画面。"""
    w, h = cfg.output_width, cfg.output_height
    parts = []
    if p["mirror"]:
        parts.append("hflip")
    # 放大后随机裁回原尺寸：破坏构图指纹
    z = p["zoom"]
    parts.append(f"scale=iw*{z}:ih*{z}")
    # 放大后可裁切余量 = W*(z-1)；按随机比例取偏移
    parts.append(
        f"crop={w}:{h}:x=(iw-{w})*{p['crop_fx']}:y=(ih-{h})*{p['crop_fy']}")
    parts.append(
        f"eq=brightness={p['brightness']}:contrast={p['contrast']}"
        f":saturation={p['saturation']}")
    parts.append(f"hue=h={p['hue']}")
    if p["noise"] > 0:
        parts.append(f"noise=alls={p['noise']}:allf=t")
    # 锐化(正)/柔化(负):改变纹理指纹
    if abs(p.get("sharpen", 0)) > 0.01:
        parts.append(f"unsharp=5:5:{p['sharpen']}:5:5:0")
    parts.append(f"fps={p['fps']}")
    parts.append("format=yuv420p")
    return ",".join(parts)
