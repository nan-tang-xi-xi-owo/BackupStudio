"""跨进程配置文件锁 —— 修「task add 说成功但任务消失」P0。

## 缺陷（用户测试 P0，8 并发只活 1 个）

`Store.save()` 是「读磁盘 → 合并 → 写回」。8 个进程几乎同时启动时：
  1. 都读到同一份旧 tasks.json 快照；
  2. `external_changed()` 按 (mtime, size) 指纹判断 → 全都认为"无外部改动"；
  3. 各自把自己的快照整体覆写 → **后写者抹掉前写者**。

实测：8 个 `task add` 并发 → 声称创建 8 个，实际只持久化 1 个。

## 修法

`save()` 全程持有**跨进程排他锁**（不是线程锁——线程锁管不了 CLI/daemon/GUI 多进程）。
用 OS 原生文件锁：POSIX 用 `fcntl.flock`，Windows 用 `msvcrt.locking`。
锁文件是 `<config>/tasks.json.lock`，只锁不写内容，开销极小。

持锁顺序：acquire → 合并 → 原子写 → release。
这样「读-改-写」成为临界区，后到者会看到前者的新增。
"""

from __future__ import annotations

import contextlib
import os
import time
from pathlib import Path
from typing import IO


class LockTimeout(RuntimeError):
    """等待配置锁超时。

    **绝不能静默降级**（并发 Agent 实测的主根因）：
    ``acquire()`` 超时若 ``return self`` 让调用方照常写，就等于在无锁状态下
    整库覆写 —— 44 进程 × 0.85s 持锁时丢 21/44 个任务。

    抛异常则调用方要么真的拿到锁，要么明确失败让用户重试，
    **不存在第三条「假装成功」的路**。
    """


class FileLock:
    """基于 OS 文件锁的跨进程排他锁（可重入不必要——调用方是单次 save）。"""

    def __init__(self, target: Path, *, timeout: float = 30.0, suffix: str = ".lock") -> None:
        self.path = Path(str(target) + suffix)
        self.timeout = timeout
        self._fh: IO[bytes] | None = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        delay = 0.005
        while True:
            try:
                self._fh = open(self.path, "a+b")
                self._lock_fd(self._fh)
                return self
            except OSError:
                if self._fh is not None:
                    with contextlib.suppress(OSError):
                        self._fh.close()
                    self._fh = None
                if time.monotonic() >= deadline:
                    # 超时**必须抛错**，不能静默降级（见 LockTimeout 文档）。
                    raise LockTimeout(
                        f"等待配置锁超时（{self.timeout:.0f}s）：{self.path}"
                    ) from None
                time.sleep(delay)
                delay = min(delay * 1.6, 0.1)

    @staticmethod
    def _lock_fd(fh: IO[bytes]) -> None:
        if os.name == "nt":
            import msvcrt

            fh.seek(0)
            # 非阻塞独占锁；已被占用会抛 OSError
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                with contextlib.suppress(OSError):
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                with contextlib.suppress(OSError):
                    fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            with contextlib.suppress(OSError):
                self._fh.close()
            self._fh = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()
