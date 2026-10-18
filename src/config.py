"""读取 config.json，合并默认值，解析各路径。

设计约定：
- 所有会进入 FFmpeg filtergraph 的路径（字体、textfile、字幕）都用 **相对项目根** 的路径，
  且 FFmpeg 以项目根为工作目录运行，从而彻底避开 Windows 盘符冒号在 filtergraph 里的转义地狱。
- ffmpeg/ffprobe 可执行文件、输入/输出媒体文件用绝对路径（它们不进 filtergraph，冒号无害）。
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# 项目根 = 本文件的上上级目录（src/config.py -> src -> 根）
ROOT = Path(__file__).resolve().parent.parent

# 环境变量 -> config 字段。只覆盖非空值，优先级高于 config.json。
ENV_OVERRIDES = {
    "audio_dir": "NOVEL_AUDIO_DIR",
    "background_dir": "NOVEL_BACKGROUND_DIR",
    "output_dir": "NOVEL_OUTPUT_DIR",
    "icon_dir": "NOVEL_ICON_DIR",
    "font_file": "NOVEL_FONT_FILE",
}

DEFAULTS = {
    # 目录：留空用默认（项目内 input/audio、input/background、output、input/tubiao）；
    # 也可填任意路径，绝对如 "F:/小说音频"，或相对项目根。
    "audio_dir": "",
    "background_dir": "",
    "output_dir": "",
    "icon_dir": "",
    "app_name": "Novel Master",
    "guide_template": "Search 「{keyword}」\non {app} APP {icon}\nto get the following",
    "guide_fontsize": 66,
    "guide_outline": 6,
    "guide_line_gap": 0.58,
    "guide_align": "center",
    "guide_icon_scale": 1.25,
    "guide_highlight_color": "#FFD400",
    "guide_top_frac": 0.15,
    "variants_per_audio": 1,
    "cooldown_days": 30,
    "daily_publish_cap": 21,
    "per_account_videos": 3,      # 每个 TikTok 号每天固定发几条
    "videos_per_audio_min": 2,    # 每条音频最少出几条视频(M 下限)
    "max_scripts_per_novel": 6,   # 每部小说改文上限(保质量,方案B少部深挖时用)
    "rewrite_mode": "hybrid",     # 改文模式: hybrid(旧文笔+新素材/保真,用户终审采纳) / simple / agent
    "delete_after_publish": True,
    "auto_feishu_sync": True,
    "publish_timezone": "America/New_York",
    "publish_slots": [[10, 11], [16, 17], [18, 19], [19, 20], [20, 21]],
    "publish_start_hour": 8,
    "publish_end_hour": 23,
    "default_caption_template": "Search {keyword} on {app}  #novel #story #booktok #fyp",
    "output_width": 1080,
    "output_height": 1920,
    "fps": 30,
    "cover_on_first_frame": True,
    "export_cover_png": True,
    "outro_seconds": 30,
    "dedup_level": "medium",
    "mirror_enabled": True,
    "audio_atempo": 1.0,
    "whisper_model": "small",
    "whisper_language": "en",
    "whisper_enabled": True,
    "whisper_prompt": "",
    "hf_endpoint": "https://hf-mirror.com",
    "hf_disable_xet": True,
    "font_file": "assets/fonts/arialbd.ttf",
    "subtitle_font_name": "Arial",
    "cover_fontsize": 62,
    "cover_max_chars_per_line": 18,
    "watermark_fontsize": 70,
    "watermark_outline": 4,
    "watermark_x": 130,
    "watermark_y": 210,
    "outro_fontsize": 58,
    "outro_max_chars_per_line": 20,
    "subtitle_mode": "word",
    "subtitle_word_fontsize": 120,
    "subtitle_word_outline": 5,
    "subtitle_center_y": 0.46,
    "subtitle_pop": True,
    "subtitle_fontsize": 56,
    "subtitle_outline": 3,
    "subtitle_margin_v": 230,
    "dedup_speed_range": [0.95, 1.05],
    "tts_xtts_speed": 1.2,
    "tts_speed_factor": 1.175,
    "target_size_mb_range": [110, 160],
    "target_size_mb": 0,
    "final_crf_range": [20, 24],
    "base_crf": 23,
    "x264_preset": "veryfast",
}


class Config:
    def __init__(self, data: dict):
        merged = dict(DEFAULTS)
        for k, v in data.items():
            if k.startswith("_"):
                continue
            merged[k] = v
        for key, env in ENV_OVERRIDES.items():
            value = os.environ.get(env)
            if value:
                merged[key] = value
        self._d = merged

        # 目录（绝对路径）。audio/background/output 可在 config 里自定义为任意路径
        # （绝对路径如 "F:/小说音频"，或相对项目根；留空用默认位置）。
        self.root = ROOT
        self.input_audio_dir = self._dir(merged.get("audio_dir"),
                                         ROOT / "input" / "audio")
        self.input_bg_dir = self._dir(merged.get("background_dir"),
                                      ROOT / "input" / "background")
        self.output_dir = self._dir(merged.get("output_dir"),
                                    ROOT / "output")
        self.input_icon_dir = self._dir(merged.get("icon_dir"),
                                        ROOT / "input" / "tubiao")
        # work/logs 固定在项目根下（work 里的字幕/图/文本要按相对路径进 filtergraph）
        self.work_dir = ROOT / "work"
        self.logs_dir = ROOT / "logs"

        # 可执行文件（绝对路径）；Docker / Linux 用 NOVEL_FFMPEG / NOVEL_FFPROBE 覆盖
        self.ffmpeg = Path(os.environ.get("NOVEL_FFMPEG")
                           or ROOT / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe")
        self.ffprobe = Path(os.environ.get("NOVEL_FFPROBE")
                            or ROOT / "tools" / "ffmpeg" / "bin" / "ffprobe.exe")

    @staticmethod
    def _dir(val, default: Path) -> Path:
        """解析目录配置：空 -> 默认；绝对路径原样用；相对路径挂到项目根下。"""
        val = (val or "").strip() if isinstance(val, str) else val
        if not val:
            return default
        p = Path(val)
        return p if p.is_absolute() else (ROOT / p)

    def __getattr__(self, name):
        # 让 cfg.output_width 等直接可读
        try:
            return self._d[name]
        except KeyError as e:
            raise AttributeError(name) from e

    def guide_text(self, keyword: str) -> str:
        return self.guide_template.format(app=self.app_name, keyword=keyword)

    def ensure_dirs(self):
        for d in (self.input_audio_dir, self.input_bg_dir, self.output_dir,
                  self.work_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    def check_env(self) -> list[str]:
        """返回缺失项列表（空 = 环境 OK）。"""
        problems = []
        if not self.ffmpeg.exists():
            problems.append(f"找不到 ffmpeg: {self.ffmpeg}")
        if not self.ffprobe.exists():
            problems.append(f"找不到 ffprobe: {self.ffprobe}")
        font = Path(self.font_file)
        font = font if font.is_absolute() else ROOT / font
        if not font.exists():
            problems.append(f"找不到字体: {font}")
        return problems


def load(path: Path | None = None) -> Config:
    path = path or Path(os.environ.get("NOVEL_CONFIG") or (ROOT / "config.json"))
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {}
    return Config(data)
