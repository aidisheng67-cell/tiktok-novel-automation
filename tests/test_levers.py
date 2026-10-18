from __future__ import annotations

import json

import pytest

from src import levers


@pytest.fixture()
def registry(tmp_path, monkeypatch):
    path = tmp_path / "levers.json"
    monkeypatch.setattr(levers, "REGISTRY", path)
    monkeypatch.setattr(levers, "LOG_DIR", tmp_path / "logs")
    return path


def test_default_registry_is_created(registry):
    reg = levers.load_registry()
    assert registry.exists()
    assert reg["active_lever"] == "hook_type"


def test_lever_column_whitelist(registry):
    assert levers.lever_column("hook_type") == "hook_type"
    registry.write_text(json.dumps({"levers": {
        "bad": {"column": "file_path; DROP TABLE videos"}}}), encoding="utf-8")
    assert levers.lever_column("bad") is None


def test_frozen_lever_always_uses_champion(registry):
    registry.write_text(json.dumps({"levers": {"hook_type": {
        "column": "hook_type", "status": "frozen", "champion": "shock"}}}),
        encoding="utf-8")
    assert levers.choose_arms("hook_type", "romance", 5,
                              ["question", "shock"]) == ["shock"] * 5


def test_choose_arms_is_deterministic_and_uses_known_arms(registry):
    arms = ["question", "shock", "confession"]
    a = levers.choose_arms("hook_type", "romance", 8, arms)
    b = levers.choose_arms("hook_type", "romance", 8, arms)
    assert a == b
    assert len(a) == 8
    assert set(a) <= set(arms)


def test_direction_order_is_used_when_present(registry):
    (registry.parent / "logs").mkdir(exist_ok=True)
    (registry.parent / "logs" / "direction_hook_type.json").write_text(
        json.dumps({"genres": {"romance": {"order": ["confession", "shock"]}}}),
        encoding="utf-8")
    picks = levers.choose_arms("hook_type", "romance", 6,
                               ["question", "shock", "confession"])
    assert picks.count("confession") >= picks.count("question")
