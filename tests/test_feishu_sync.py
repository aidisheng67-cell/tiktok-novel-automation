from __future__ import annotations

import feishu_sync


def test_txt_flattens_richtext():
    assert feishu_sync._txt([{"text": "Hello "}, {"text": "World"}]) == "Hello World"
    assert feishu_sync._txt(" value ") == "value"
    assert feishu_sync._txt(None) == ""


def test_int_parses_numbers_and_falls_back():
    assert feishu_sync._int("3", 5) == 3
    assert feishu_sync._int("4.0", 5) == 4
    assert feishu_sync._int([{"text": "7"}], 5) == 7
    assert feishu_sync._int("", 5) == 5
    assert feishu_sync._int(None, 5) == 5
    assert feishu_sync._int("abc", 5) == 5


def test_missing_credentials_raise_readable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(feishu_sync, "_CFG", {})
    monkeypatch.setattr(feishu_sync, "_tok", None)
    try:
        feishu_sync.token()
    except RuntimeError as e:
        assert "缺少飞书凭证" in str(e)
    else:
        raise AssertionError("expected RuntimeError")
