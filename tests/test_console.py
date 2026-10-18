from __future__ import annotations

import json

import console


def _headers(**kw):
    return {k.replace("_", "-"): v for k, v in kw.items()}


def test_origin_allowed_accepts_local_page():
    assert console.origin_allowed(_headers(Host="127.0.0.1:8770"))
    assert console.origin_allowed(_headers(Host="localhost:8770"))
    assert console.origin_allowed(_headers(
        Host="127.0.0.1:8770", Origin="http://127.0.0.1:8770"))


def test_origin_allowed_rejects_foreign_host_and_origin():
    assert not console.origin_allowed(_headers(Host="evil.example"))
    assert not console.origin_allowed(_headers(
        Host="127.0.0.1:8770", Origin="http://evil.example"))


def test_api_get_config_masks_credentials(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"app_name": "Demo"}), encoding="utf-8")
    creds = {name: tmp_path / f"{name}.json" for name in ("deepseek", "feishu", "feixing")}
    creds["deepseek"].write_text(json.dumps({
        "api_key": "sk-real-secret", "model": "deepseek-chat"}), encoding="utf-8")

    monkeypatch.setattr(console, "CONFIG", config_path)
    monkeypatch.setattr(console, "CREDS", creds)

    payload = console.api_get_config()
    blob = json.dumps(payload, ensure_ascii=False)
    assert "sk-real-secret" not in blob
    assert payload["creds"]["deepseek"]["api_key"] == "***"
    assert payload["creds"]["deepseek"]["model"] == "deepseek-chat"
    assert payload["fields"]["app_name"] == "Demo"


def test_api_save_config_ignores_masked_placeholders(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"app_name": "Old"}), encoding="utf-8")
    deepseek = tmp_path / "deepseek.json"
    deepseek.write_text(json.dumps({
        "api_key": "sk-real", "model": "deepseek-chat"}), encoding="utf-8")

    monkeypatch.setattr(console, "ROOT", tmp_path)
    monkeypatch.setattr(console, "CONFIG", config_path)
    monkeypatch.setattr(console, "CREDS", {
        "deepseek": deepseek, "feishu": tmp_path / "feishu.json",
        "feixing": tmp_path / "feixing.json"})

    console.api_save_config({
        "fields": {"app_name": "New", "per_account_videos": "2"},
        "creds": {"deepseek": {"api_key": "***", "model": "deepseek-reasoner"}},
    })

    saved_cfg = json.loads(config_path.read_text(encoding="utf-8"))
    assert saved_cfg["app_name"] == "New"
    assert saved_cfg["per_account_videos"] == 2

    saved_creds = json.loads(deepseek.read_text(encoding="utf-8"))
    assert saved_creds["api_key"] == "sk-real"          # 占位符没覆盖真实值
    assert saved_creds["model"] == "deepseek-reasoner"
