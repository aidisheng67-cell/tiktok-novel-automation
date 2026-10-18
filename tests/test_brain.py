from __future__ import annotations

import pytest

from src import brain


@pytest.fixture()
def db(tmp_path, monkeypatch):
    path = tmp_path / "state.db"
    monkeypatch.setattr(brain, "DB_PATH", path)
    brain.init_db()
    return path


def test_init_db_is_idempotent(db):
    brain.init_db()
    brain.init_db()
    con = brain.connect()
    tables = {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    con.close()
    assert {"keywords", "audios", "captions", "videos", "accounts", "publish_log"} <= tables


def test_register_video_and_pending(db):
    brain.register_video("v1", "Alpha", 1, "/tmp/v1.mp4", size_mb=12.5,
                         caption_text="caption", hook_type="shock", genre="romance")
    rows = brain.pending_videos()
    assert len(rows) == 1
    assert rows[0]["video_id"] == "v1"
    assert rows[0]["caption_id"] is not None
    assert rows[0]["hook_type"] == "shock"


def test_register_video_upsert_keeps_single_row(db):
    brain.register_video("v1", "Alpha", 1, "/tmp/a.mp4")
    brain.register_video("v1", "Alpha", 1, "/tmp/b.mp4", size_mb=9.0)
    rows = brain.pending_videos()
    assert len(rows) == 1
    assert rows[0]["file_path"] == "/tmp/b.mp4"


def test_next_seq(db):
    assert brain.next_seq("Alpha") == 1
    brain.register_video("v1", "Alpha", 1, "/tmp/a.mp4")
    assert brain.next_seq("Alpha") == 2


def test_publish_rules(db):
    brain.upsert_account("acc1", name="A")
    assert brain.can_publish("acc1", "Alpha", cooldown_days=30)
    brain.record_publish("v1", "acc1", "Alpha", status="scheduled")
    assert not brain.can_publish("acc1", "Alpha", cooldown_days=30)
    assert brain.count_today("acc1") == 1
    assert brain.count_today_all() == 1


def test_failed_upload_does_not_consume_quota(db):
    brain.upsert_account("acc1", name="A")
    brain.record_publish("v1", "acc1", "Alpha", status="failed", error="timeout")
    assert brain.can_publish("acc1", "Alpha", cooldown_days=30)
    assert brain.count_today("acc1") == 0


def test_status_report_shape(db):
    brain.upsert_account("acc1", name="A", daily_min=2, daily_max=3)
    brain.register_video("v1", "Alpha", 1, "/tmp/a.mp4")
    rep = brain.status_report()
    assert rep["keywords"] == 1
    assert rep["videos_total"] == 1
    assert rep["videos_pending"] == 1
    assert rep["accounts_active"] == 1
    assert rep["accounts_detail"][0]["quota"] == "2-3"
