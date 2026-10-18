"""在后台线程里运行阻塞任务，并通过 Qt 信号回到 UI 线程。"""

from __future__ import annotations

import logging
import threading
import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, Signal

log = logging.getLogger("backupstudio.worker")


class Worker(QObject):
    """把一个函数放到独立线程执行。

    用法::

        w = Worker(self.engine.run, task)
        w.finished.connect(self._on_done)
        w.failed.connect(self._on_error)
        w.start()
    """

    finished = Signal(object)
    failed = Signal(str, str)  # message, traceback
    progress = Signal(dict)

    def __init__(self, fn: Callable[..., Any], *args, parent: QObject | None = None, **kwargs) -> None:
        super().__init__(parent)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._thread: threading.Thread | None = None
        self.cancelled = threading.Event()

    def cancel(self) -> None:
        self.cancelled.set()

    def is_cancelled(self) -> bool:
        return self.cancelled.is_set()

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True, name="bs-worker")
        self._thread.start()

    def _run(self) -> None:
        try:
            result = self._fn(*self._args, **self._kwargs)
        except BaseException as exc:  # 包括取消
            log.info("后台任务异常: %s", exc)
            self.failed.emit(f"{type(exc).__name__}: {exc}", traceback.format_exc())
            return
        self.finished.emit(result)


def emit_progress(worker: Worker, payload: dict) -> None:
    """给进度回调安全地转发到 UI 线程。"""
    try:
        worker.progress.emit(dict(payload))
    except RuntimeError:
        pass
