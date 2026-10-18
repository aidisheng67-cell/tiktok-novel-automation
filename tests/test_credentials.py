from __future__ import annotations

import json

from src import credentials


def test_env_overrides_json_and_defaults(tmp_path):
    (tmp_path / "deepseek.json").write_text(json.dumps({
        "api_key": "from-json",
        "base_url": "https://json.example",
        "model": "json-model",
    }), encoding="utf-8")
    monkeypatch = _monkeypatch({
        "DEEPSEEK_API_KEY": "from-env",
        "DEEPSEEK_BASE_URL": "",
    })
    try:
        cfg = credentials.load("deepseek", root=tmp_path)
    finally:
        monkeypatch()
    assert cfg["api_key"] == "from-env"      # 环境变量优先
    assert cfg["base_url"] == "https://json.example"  # 空环境变量不覆盖
    assert cfg["model"] == "json-model"


def test_defaults_apply_without_json(tmp_path):
    cfg = credentials.load("deepseek", root=tmp_path)
    assert cfg["base_url"] == "https://api.deepseek.com"
    assert cfg["model"] == "deepseek-chat"
    assert "api_key" not in cfg


def test_missing_lists_blank_fields():
    cfg = {"api_key": "  ", "team_id": "t1"}
    assert credentials.missing("feixing", cfg, ("api_key", "team_id")) == ["api_key"]


def test_redact_text_hides_secret_values_and_query_keys():
    text = "failed api_key=sk-abcdef123456 token=xyz789"
    out = credentials.redact_text(text, secrets=("sk-abcdef123456",))
    assert "sk-abcdef123456" not in out
    assert "xyz789" not in out
    assert "api_key=***" in out


def test_redact_masks_secret_field_names():
    data = {"api_key": "sk-real", "app_secret": "s", "model": "deepseek-chat",
            "note": "call with token=abc12345"}
    out = credentials.redact(data)
    assert out["api_key"] == "***"
    assert out["app_secret"] == "***"
    assert out["model"] == "deepseek-chat"
    assert "abc12345" not in out["note"]


def test_save_roundtrip(tmp_path):
    path = credentials.save("feishu", {"app_id": "cli_1"}, root=tmp_path)
    assert path == tmp_path / "feishu.json"
    assert json.loads(path.read_text(encoding="utf-8"))["app_id"] == "cli_1"


def _monkeypatch(values: dict):
    import os
    old = {k: os.environ.get(k) for k in values}

    def restore():
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    for k, v in values.items():
        os.environ[k] = v
    return restore
