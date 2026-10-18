"""单实例锁：避免定时任务、控制台和手动命令同时操作同一个 state.db。

Windows 用 OpenProcess 判断 PID 是否存活，POSIX 用 signal 0。
锁文件里保存 PID，进程崩溃后残留的锁会被下一次启动自动回收。
"""
from __future__ import annotations

import ctypes
import os


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class SingleInstance:
    def __init__(self, path: str = "./_work/pipeline.lock"):
        self.path = path
        self.fd = None

    def _holder(self) -> int:
        try:
            with open(self.path, encoding="utf-8") as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def _stale(self) -> bool:
        pid = self._holder()
        return pid <= 0 or not _pid_alive(pid)

    def acquire(self) -> bool:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        for _ in range(2):
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
                os.write(self.fd, str(os.getpid()).encode("ascii"))
                return True
            except FileExistsError:
                if not self._stale():
                    return False
                try:
                    os.unlink(self.path)
                except OSError:
                    return False
        return False

    def release(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError(
                f"已有实例在运行（pid={self._holder()}），锁文件：{self.path}")
        return self

    def __exit__(self, *exc):
        self.release()
        return False
