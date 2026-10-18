from __future__ import annotations

import json

from src import config as config_mod
from src.config import Config


def test_defaults():
    cfg = Config({})
    assert cfg.output_width == 1080
    assert cfg.output_height == 1920
    assert cfg.dedup_level == "medium"
    assert cfg.per_account_videos == 3
    assert cfg.publish_timezone == "America/New_York"


def test_json_overrides_defaults():
    cfg = Config({"app_name": "Demo App", "per_account_videos": 2})
    assert cfg.app_name == "Demo App"
    assert cfg.per_account_videos == 2
    assert cfg.output_width == 1080


def test_env_overrides_json(tmp_path, monkeypatch):
    monkeypatch.setenv("NOVEL_OUTPUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("NOVEL_CONFIG", str(tmp_path / "missing.json"))
    cfg = config_mod.load()
    assert cfg.output_dir == tmp_path / "out"


def test_relative_dir_anchored_to_root():
    cfg = Config({"output_dir": "my_output"})
    assert cfg.output_dir == config_mod.ROOT / "my_output"


def test_blank_dir_uses_default():
    cfg = Config({"output_dir": ""})
    assert cfg.output_dir == config_mod.ROOT / "output"


def test_load_reads_explicit_path(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"app_name": "From File"}), encoding="utf-8")
    cfg = config_mod.load(path)
    assert cfg.app_name == "From File"


def test_guide_text_formats_keyword():
    cfg = Config({"app_name": "Good Novel",
                  "guide_template": "Search {keyword} on {app}"})
    assert cfg.guide_text("Alpha") == "Search Alpha on Good Novel"


def test_check_env_reports_missing_binaries_and_font():
    cfg = Config({"font_file": "does/not/exist.ttf"})
    problems = cfg.check_env()
    assert any("ffmpeg" in p for p in problems)
    assert any("字体" in p for p in problems)
