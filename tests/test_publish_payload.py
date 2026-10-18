from __future__ import annotations

import pytest

import feixing_publish


def test_publish_task_labels_ai_content(monkeypatch):
    captured = {}

    def fake_api(cfg, path, body):
        captured["path"] = path
        captured["body"] = body
        return {"task_id": "task-1"}

    monkeypatch.setattr(feixing_publish, "api_json", fake_api)
    cfg = {"team_id": "team-1", "api_key": "key-1"}
    task = feixing_publish.publish_task(cfg, "media-1", "video-1", "Title", "Desc")

    assert task == "task-1"
    assert captured["path"] == "/media/task/create/v2"
    body = captured["body"]
    assert body["team_id"] == "team-1"
    assert body["media_ids"] == ["media-1"]
    assert body["is_draft"] is True
    # 内容经 AI 改写/配音/合成，必须如实标注
    assert body["tiktok_biz_data"]["is_ai_generated"] is True


def test_publish_task_supports_cover_and_schedule(monkeypatch):
    captured = {}

    def fake_api(cfg, path, body):
        captured["body"] = body
        return {"task_id": "task-2"}

    monkeypatch.setattr(feixing_publish, "api_json", fake_api)
    feixing_publish.publish_task({"team_id": "t"}, "m", "v", "T", "D",
                                 cover_id="cover-1", draft=False,
                                 pub_time_ms=1234567890)
    body = captured["body"]
    assert body["common_data"]["cover_id"] == "cover-1"
    assert body["is_draft"] is False
    assert body["pub_time"] == 1234567890


def test_check_redacts_secrets_in_errors(monkeypatch):
    class FakeResp:
        status_code = 200
        text = "api_key=sk-super-secret"

        def json(self):
            return {"code": 40001, "message": "bad key sk-super-secret",
                    "request_id": "r1"}

    monkeypatch.setattr(feixing_publish, "_SECRETS", ("sk-super-secret",))
    with pytest.raises(RuntimeError) as exc:
        feixing_publish._check(FakeResp(), "/media/task")
    assert "sk-super-secret" not in str(exc.value)
    assert "***" in str(exc.value)
