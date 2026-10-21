"""跨进程任务锁：防止 GUI 与 daemon 同时向同一目标执行同一任务。

背景（审查 P0-6/F2 实测）：引擎的 threading.Lock 只在进程内有效；
两进程同秒同名会产出坏包、双方都报 success。本模块提供基于文件系统的
目录锁（target 目录下 `.backupstudio/task-<id>.lock`），满足四条件：
* 落在文件系统上（跨进程有效）；
* 可判定持有者存活（同机 pid + psutil；否则租约超时）→ 崩溃自愈；
* 内容可读（pid/host/user/时间），等待时可解释；
* 接管有防竞态（rename 陈旧锁 + O_EXCL 重抢）。
"""

from __future__ import annotations

import contextlib
import getpass
import json
import logging
import os
import socket
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("backupstudio.locks")

LOCK_DIRNAME = ".backupstudio"
STALE_LEASE_SECONDS = 30 * 60  # 跨机（网络盘）场景的租约上限


class LockError(RuntimeError):
    pass


class LockTimeout(LockError):
    pass


@dataclass
class LockInfo:
    pid: int
    host: str
    user: str
    task_id: str
    started_at: float


def _lock_path(directory: Path, task_id: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id) or "task"
    return Path(directory) / LOCK_DIRNAME / f"task-{safe}.lock"


def _read_info(path: Path) -> LockInfo | None:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return LockInfo(
            pid=int(data.get("pid", -1)),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            task_id=str(data.get("task_id", "")),
            started_at=float(data.get("started_at", 0.0)),
        )
    except (OSError, ValueError, TypeError):
        return None


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        import psutil  # type: ignore[import-not-found]

        return psutil.pid_exists(pid)
    except ImportError:  # pragma: no cover
        try:
            os.kill(pid, 0)  # Windows 上对死进程不一定抛，故优先 psutil
            return True
        except OSError:
            return False


def _is_stale(info: LockInfo) -> bool:
    """持有者已死（同机 pid 不在）或超租约（跨机场景）。"""
    mine = socket.gethostname()
    if info.host and info.host != mine:
        return (time.time() - info.started_at) > STALE_LEASE_SECONDS
    return not _pid_alive(info.pid)


class DirLock:
    """任务级文件锁（上下文管理器）。"""

    def __init__(self, directory: Path, task_id: str, *, timeout: float = 0.0, on_wait=None) -> None:
        self.directory = Path(directory)
        self.task_id = task_id
        self.timeout = timeout
        self.on_wait = on_wait
        self.path = _lock_path(self.directory, task_id)
        self._held = False

    # ------------------------------------------------------------------
    def acquire(self) -> "DirLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + max(0.0, self.timeout)
        while True:
            if self._try_acquire():
                self._held = True
                return self
            info = _read_info(self.path)
            if info is not None and _is_stale(info):
                # 陈旧锁：改名旁置（防接管竞态），再重抢
                with contextlib.suppress(OSError):
                    self.path.replace(self.path.with_suffix(".lock.stale"))
                continue
            if time.monotonic() >= deadline:
                who = f"{info.user}@{info.host} (pid {info.pid})" if info else "未知进程"
                raise LockTimeout(f"任务正在被另一个进程执行：{who}")
            if self.on_wait is not None:
                with contextlib.suppress(Exception):
                    self.on_wait(who if info else "另一个进程")
            time.sleep(0.5)

    def _try_acquire(self) -> bool:
        payload = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "user": _user(),
            "task_id": self.task_id,
            "started_at": time.time(),
        }
        text = json.dumps(payload)
        try:
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return False
        except OSError as exc:
            raise LockError(f"无法创建锁文件 {self.path}：{exc}") from exc
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(self.path)
            raise
        return True

    def release(self) -> None:
        if not self._held:
            return
        info = _read_info(self.path)
        if info is not None and info.pid == os.getpid() and info.host == socket.gethostname():
            with contextlib.suppress(OSError):
                self.path.unlink()
        self._held = False

    # ------------------------------------------------------------------
    def __enter__(self) -> "DirLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        self.release()


def _user() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001
        return "unknown"


def acquire_dir_lock(directory: Path | str, task_id: str, *, timeout: float = 0.0, on_wait=None) -> DirLock:
    """便捷函数：获取任务目录锁（失败抛 LockTimeout）。"""
    return DirLock(Path(directory), task_id, timeout=timeout, on_wait=on_wait).acquire()
