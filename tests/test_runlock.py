from __future__ import annotations

import os

import pytest

from runlock import SingleInstance


def test_acquire_release_reacquire(tmp_path):
    path = tmp_path / "x.lock"
    a = SingleInstance(str(path))
    assert a.acquire() is True
    assert path.exists()
    a.release()
    assert not path.exists()

    b = SingleInstance(str(path))
    assert b.acquire() is True
    b.release()


def test_second_instance_is_rejected(tmp_path):
    path = tmp_path / "x.lock"
    a = SingleInstance(str(path))
    b = SingleInstance(str(path))
    assert a.acquire() is True
    try:
        assert b.acquire() is False
        with pytest.raises(RuntimeError, match="已有实例"):
            with b:
                pass
    finally:
        a.release()


def test_stale_lock_is_reclaimed(tmp_path):
    path = tmp_path / "x.lock"
    path.write_text("0", encoding="utf-8")     # 0 = 一定不存在/已退出的进程
    lock = SingleInstance(str(path))
    assert lock.acquire() is True
    assert path.read_text(encoding="utf-8") == str(os.getpid())
    lock.release()


def test_nested_directory_is_created(tmp_path):
    path = tmp_path / "a" / "b" / "x.lock"
    with SingleInstance(str(path)):
        assert path.exists()
    assert not path.exists()
