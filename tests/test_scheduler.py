from __future__ import annotations

import datetime as dt
import random
from zoneinfo import ZoneInfo

import pytest

from src import brain, scheduler
from src.config import Config

TZ = ZoneInfo("America/New_York")
NOW = dt.datetime(2026, 10, 19, 8, 0, tzinfo=TZ)


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(brain, "DB_PATH", tmp_path / "state.db")
    brain.init_db()
    brain.upsert_account("a1", name="Alpha", daily_min=2, daily_max=3)
    brain.upsert_account("a2", name="Bravo", daily_min=2, daily_max=3)
    brain.upsert_account("a3", name="Off", active=0)
    for kw in ("K1", "K2", "K3", "K4"):
        for i in range(3):
            brain.register_video(f"{kw}-{i}", kw, i + 1, f"/tmp/{kw}-{i}.mp4",
                                 caption_text=f"caption {kw} {i}")
    return tmp_path / "state.db"


def make_cfg(**kw):
    base = {"daily_publish_cap": 21, "per_account_videos": 2,
            "cooldown_days": 30, "publish_slots": [[10, 11], [16, 17], [18, 19]]}
    base.update(kw)
    return Config(base)


def test_plan_is_deterministic_and_bounded(db):
    cfg = make_cfg()
    p1 = scheduler.plan_day(cfg, random.Random(7), now=NOW)
    p2 = scheduler.plan_day(cfg, random.Random(7), now=NOW)
    assert [(x["video_id"], x["account_id"], x["pub_time"]) for x in p1] == \
           [(x["video_id"], x["account_id"], x["pub_time"]) for x in p2]
    assert len(p1) == 4                     # 2 个启用账号 × 2 条
    assert len({x["video_id"] for x in p1}) == len(p1)
    assert {x["account_id"] for x in p1} == {"a1", "a2"}


def test_per_account_limits_and_unique_keywords(db):
    cfg = make_cfg()
    plan = scheduler.plan_day(cfg, random.Random(3), now=NOW, per_account=2)
    for acc in ("a1", "a2"):
        rows = [x for x in plan if x["account_id"] == acc]
        assert len(rows) == 2
        assert len({x["keyword"] for x in rows}) == 2


def test_cooldown_excludes_recent_keyword(db):
    brain.record_publish("K1-0", "a1", "K1", status="scheduled")
    cfg = make_cfg()
    plan = scheduler.plan_day(cfg, random.Random(1), now=NOW, per_account=3)
    assert not any(x["account_id"] == "a1" and x["keyword"] == "K1" for x in plan)


def test_global_cap_zero_yields_empty_plan(db):
    cfg = make_cfg(daily_publish_cap=0)
    assert scheduler.plan_day(cfg, random.Random(1), now=NOW) == []


def test_today_quota_reduces_remaining(db):
    brain.record_publish("done-1", "a1", "K4", status="scheduled")
    cfg = make_cfg(per_account_videos=2)
    plan = scheduler.plan_day(cfg, random.Random(1), now=NOW)
    assert len([x for x in plan if x["account_id"] == "a1"]) == 1


def test_caption_uses_bound_caption(db):
    cfg = make_cfg()
    plan = scheduler.plan_day(cfg, random.Random(5), now=NOW, per_account=1)
    assert plan and plan[0]["caption"].startswith("caption ")


def test_slot_time_rolls_to_tomorrow_when_past():
    t = scheduler._slot_time(NOW, 7, 0, 0)
    assert t.date() == NOW.date() + dt.timedelta(days=1)
    t2 = scheduler._slot_time(NOW, 20, 0, 0)
    assert t2.date() == NOW.date()
