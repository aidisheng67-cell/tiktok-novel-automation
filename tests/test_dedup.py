from __future__ import annotations

import random

from src import dedup
from src.config import Config


def test_params_within_level_ranges():
    cfg = Config({"dedup_level": "medium", "mirror_enabled": True})
    p = dedup.random_params(cfg, random.Random(42))
    assert 1.02 <= p["zoom"] <= 1.06
    assert 0.95 <= p["speed"] <= 1.05
    assert 20 <= p["crf"] <= 24
    assert 4 <= p["noise"] <= 8
    assert p["fps"] in (30, 29, 25)
    assert p["mirror"] is True
    assert 0.0 <= p["crop_fx"] <= 1.0


def test_params_are_reproducible_and_vary():
    cfg = Config({})
    a = dedup.random_params(cfg, random.Random(1))
    b = dedup.random_params(cfg, random.Random(1))
    c = dedup.random_params(cfg, random.Random(2))
    assert a == b
    assert a != c


def test_chain_contains_core_transforms():
    cfg = Config({})
    p = dedup.random_params(cfg, random.Random(9))
    chain = dedup.dedup_video_chain(cfg, p)
    assert chain.startswith("hflip,")
    assert "scale=iw*" in chain
    assert "crop=1080:1920" in chain
    assert "eq=brightness=" in chain
    assert chain.endswith("format=yuv420p")


def test_mirror_disabled_removes_hflip():
    cfg = Config({"mirror_enabled": False})
    chain = dedup.dedup_video_chain(cfg, dedup.random_params(cfg, random.Random(1)))
    assert "hflip" not in chain
