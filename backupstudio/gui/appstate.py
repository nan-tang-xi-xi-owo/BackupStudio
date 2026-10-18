"""应用级 Qt 状态：共享的仓库、引擎、调度器状态、通知与主题。"""

from __future__ import annotations

import dataclasses
import logging
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..config import AppSettings, BackupTask
from ..engine import TaskEngine, read_history
from ..notify import in_quiet_hours, native_notification
from ..paths import config_dir
from ..schedule import describe_next, due, parse_ts
from ..store import TaskStore
from . import DARK, LIGHT, Palette

log = logging.getLogger("backupstudio.appstate")


class AppState(QObject):
    """GUI 全局状态。

    负责：
    * 加载/保存任务仓库；
    * 每 20 秒检查一次是否有任务到期（GUI 打开时也负责调度，即使守护进程没起来）；
    * 暴露主题、通知设置。
    """

    tasks_changed = Signal()
    task_updated = Signal(str)
    progress_changed = Signal(str, dict)  # task_id, progress payload
    theme_changed = Signal(object)  # Palette
    notified = Signal(str, str, str)  # title, message, level
    run_started = Signal(str)
    run_finished = Signal(object)  # ExecutionResult

    def __init__(self, store: TaskStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.engine = TaskEngine(log_fn=lambda msg: log.info(msg))
        self._running: set[str] = set()
        self._trigger_timer = QTimer(self)
        self._trigger_timer.setInterval(20_000)
        self._trigger_timer.timeout.connect(self.check_due)
        self.palette: Palette = self._resolve_palette()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # 主题
    # ------------------------------------------------------------------
    def _resolve_palette(self) -> Palette:
        from PySide6.QtWidgets import QApplication

        mode = getattr(self.store.settings, "theme_mode", "system")
        if mode == "dark":
            return DARK
        if mode == "light":
            return LIGHT
        app = QApplication.instance()
        if app is not None:
            bg = app.palette().color(app.palette().ColorRole.Window)
            # 亮度判断系统深浅色
            lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()
            return DARK if lum < 128 else LIGHT
        return LIGHT

    def set_theme(self, mode: str) -> None:
        self.store.settings.theme_mode = mode
        self.store.save()
        self.palette = self._resolve_palette()
        self.theme_changed.emit(self.palette)

    # ------------------------------------------------------------------
    # 任务
    # ------------------------------------------------------------------
    def tasks(self) -> list[BackupTask]:
        return self.store.list_tasks()

    def get(self, task_id: str) -> BackupTask | None:
        return self.store.get(task_id)

    def save_task(self, task: BackupTask) -> None:
        self.store.upsert(task)
        self.store.save()
        self.task_updated.emit(task.id)
        self.tasks_changed.emit()

    def delete_task(self, task_id: str) -> None:
        self.store.delete(task_id)
        self.store.save()
        self.tasks_changed.emit()

    def save_settings(self, settings: AppSettings) -> None:
        self.store.settings = settings
        self.store.save()

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    # ------------------------------------------------------------------
    # 调度
    # ------------------------------------------------------------------
    def start_scheduler(self) -> None:
        self._trigger_timer.start()

    def stop_scheduler(self) -> None:
        self._trigger_timer.stop()

    def check_due(self) -> None:
        now = datetime.now()
        for t in self.tasks():
            if not t.enabled or t.schedule.type == "manual":
                continue
            if t.id in self._running:
                continue
            if due(t, now, parse_ts(t.last_run)):
                log.info("计划触发：%s（%s）", t.name, t.schedule.describe())
                self.run_task(t.id, trigger="schedule")

    def next_run_text(self, task: BackupTask) -> str:
        return describe_next(task)

    def run_task(self, task_id: str, trigger: str = "manual", on_done=None, force_retry: bool = False) -> bool:  # noqa: ANN001
        """在后台线程执行任务。返回是否成功启动。"""
        task = self.store.get(task_id)
        if task is None or task_id in self._running:
            return False
        from .worker import Worker

        self._running.add(task_id)
        self.run_started.emit(task_id)
        task.last_status = "running"
        self.task_updated.emit(task_id)

        def _pipeline_progress(payload: dict) -> None:
            """压缩线程回调 → Qt 信号（自动跨线程排队到 UI 线程）。"""
            try:
                self.progress_changed.emit(task_id, dict(payload))
            except RuntimeError:
                pass

        exec_task = task
        if force_retry:
            forced_opts = dataclasses.replace(exec_task.options, force=True)
            exec_task = dataclasses.replace(exec_task, options=forced_opts)
        # 关键：把「取消」信号传进引擎，否则「停止」按钮不起作用
        worker = Worker(
            self.engine.run,
            exec_task,
            trigger=trigger,
            progress_cb=_pipeline_progress,
            cancel=lambda: worker_ref.is_cancelled(),
        )
        worker_ref = worker  # 防止被回收


        def _done(result) -> None:  # noqa: ANN001
            with self._lock:
                self._running.discard(task_id)
            rec = result.record
            t = self.store.get(task_id)
            if t is not None:
                t.last_run = rec.started_at
                t.last_status = rec.status
                t.last_message = rec.errors[0] if rec.errors else result.message
                t.last_archive = rec.archive
                t.last_duration = rec.duration
                self.store.upsert(t)
                self.store.save()
            self.task_updated.emit(task_id)
            self.run_finished.emit(result)
            self._notify_result(result)
            if on_done is not None:
                try:
                    on_done(result)
                except Exception:
                    log.exception("任务回调异常")

        def _failed(message: str, tb: str) -> None:
            with self._lock:
                self._running.discard(task_id)
            log.error("任务线程异常: %s\n%s", message, tb)
            t = self.store.get(task_id)
            if t is not None:
                t.last_status = "failure"
                t.last_message = message
                self.store.save()
            self.task_updated.emit(task_id)
            self.notified.emit("备份失败", f"{task.name}: {message}", "error")

        worker.finished.connect(_done)
        worker.failed.connect(_failed)
        # 保存引用，确保运行期间不被 GC
        setattr(self, f"_worker_{task_id}", worker_ref)
        worker.start()
        return True

    def _notify_result(self, result) -> None:  # noqa: ANN001
        """按设置弹出托盘通知 + 系统通知。"""
        notif = self.store.settings.notifications
        if not notif.enabled:
            return
        if in_quiet_hours(notif.quiet_hours, notif.quiet_start, notif.quiet_end):
            return
        rec = result.record
        ok = rec.status == "success"
        if ok and "success" not in notif.events:
            return
        if not ok and "failure" not in notif.events:
            return
        title = f"{'备份完成' if ok else '备份失败'}：{rec.task_name}"
        if ok:
            from ..util import human_size

            msg = f"{rec.files} 个文件 · {human_size(rec.archive_bytes)} · {rec.duration:.1f}s"
        else:
            msg = (rec.errors[0] if rec.errors else result.message)[:180]
        # 系统通知（Windows 由托盘气泡替代，这里不重复调用）
        import sys

        if not sys.platform.startswith("win") and notif.native:
            native_notification("BackupStudio", f"{title}\n{msg}", "normal" if ok else "critical")
        self.notified.emit(title, msg, "success" if ok else "error")

    # ------------------------------------------------------------------
    def history(self) -> list[dict]:
        return read_history(300)


def config_directory() -> Path:
    return config_dir()
