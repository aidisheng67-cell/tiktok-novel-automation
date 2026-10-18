from __future__ import annotations

from types import SimpleNamespace

from src import subtitle
from src.config import Config


def test_fmt_ass():
    assert subtitle._fmt_ass(0) == "0:00:00.00"
    assert subtitle._fmt_ass(1.239) == "0:00:01.24"
    assert subtitle._fmt_ass(3661.5) == "1:01:01.50"
    assert subtitle._fmt_ass(-5) == "0:00:00.00"


def test_ass_text_cleans_leading_punctuation_and_newlines():
    assert subtitle._ass_text("  ,Hello\nworld ") == "Hello\\Nworld"
    assert subtitle._ass_text("{tag}") == "(tag)"


def test_clean_word():
    assert subtitle._clean_word("  ,Hello! ") == "Hello"
    assert subtitle._clean_word("don't,") == "don't"
    assert subtitle._clean_word("") == ""


def test_ass_header_uses_video_resolution():
    cfg = Config({})
    header = subtitle._ass_header(cfg, fontsize=120, outline=5, alignment=5, margin_v=0)
    assert "PlayResX: 1080" in header
    assert "PlayResY: 1920" in header
    assert "Style: Default,Arial,120" in header


def test_sentence_events_skip_empty_segments():
    cfg = Config({})
    segments = [
        SimpleNamespace(start=0.0, end=1.5, text="Hello there."),
        SimpleNamespace(start=1.5, end=2.0, text="   "),
    ]
    events = subtitle._sentence_events(cfg, segments)
    assert len(events) == 1
    assert events[0].startswith("Dialogue: 0,0:00:00.00,0:00:01.50,Default")
    assert "Hello there." in events[0]


def test_word_events_hold_until_next_word_and_cap_long_gaps():
    cfg = Config({"subtitle_pop": False})
    seg = SimpleNamespace(start=0.0, end=1.0, words=[
        SimpleNamespace(start=0.0, end=0.4, word="Hello"),
        SimpleNamespace(start=5.0, end=5.4, word="world"),
    ])
    events = subtitle._word_events(cfg, [seg])
    assert len(events) == 2
    assert "0:00:00.00,0:00:01.60" in events[0]   # 上一词结束 0.4s，长停顿时最多多停 1.2s
    assert "\\pos(540," in events[0]
    assert "Hello" in events[0] and "world" in events[1]
