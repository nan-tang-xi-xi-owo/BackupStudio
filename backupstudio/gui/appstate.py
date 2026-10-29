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

# 外部改动探测间隔（W-A7-2：GUI 打开期间 CLI/守护进程写 tasks.json 必须被看到）
EXTERNAL_POLL_MS = 5_000
# 守护进程状态探测间隔（旧账 F9：界面看不到守护进程在不在跑）
DAEMON_POLL_MS = 20_000


class AppState(QObject):
    """GUI 全局状态。

    负责：
    * 加载/保存任务仓库；
    * 每 20 秒检查一次是否有任务到期（GUI 打开时也负责调度，即使守护进程没起来）；
    * 每 5 秒探测 tasks.json 是否被外部（CLI/守护进程）改动并三方合并进内存，
      顺带探测守护进程运行状态（W-A7-2 / F9 旧账）；
    * 暴露主题、通知设置。
    """

    tasks_changed = Signal()
    task_updated = Signal(str)
    progress_changed = Signal(str, dict)  # task_id, progress payload
    theme_changed = Signal(object)  # Palette
    notified = Signal(str, str, str)  # title, message, level
    run_started = Signal(str)
    run_finished = Signal(object)  # ExecutionResult
    external_reloaded = Signal(object)  # dict：本次从磁盘吸收到的改动摘要
    daemon_state_changed = Signal(object)  # dict：守护进程状态快照

    def __init__(self, store: TaskStore, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.store = store
        self.engine = TaskEngine(log_fn=lambda msg: log.info(msg))
        self._running: set[str] = set()
        self._trigger_timer = QTimer(self)
        self._trigger_timer.setInterval(20_000)
        self._trigger_timer.timeout.connect(self.check_due)
        # 外部改动探测（tasks.json 被 CLI/守护进程写过就必须重读，否则任何一次
        # GUI 保存都会以陈旧内存全量覆写磁盘、静默抹掉外部任务）
        self._external_timer = QTimer(self)
        self._external_timer.setInterval(EXTERNAL_POLL_MS)
        self._external_timer.timeout.connect(self.poll_external_changes)
        # 守护进程状态：IPC 查询会阻塞数十~数百毫秒，放后台线程做
        self._daemon_timer = QTimer(self)
        self._daemon_timer.setInterval(DAEMON_POLL_MS)
        self._daemon_timer.timeout.connect(self.poll_daemon_status)
        self._daemon_probe_busy = threading.Lock()
        self.daemon_info: dict = {}
        self.palette: Palette = self._resolve_palette()
        self._lock = threading.Lock()
        #: 历史记录缓存：((mtime_ns, size), rows)
        self._history_cache: tuple[tuple[int, int], list[dict]] | None = None

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
        self._external_timer.start()
        self._daemon_timer.start()
        self.poll_external_changes()

    def stop_scheduler(self) -> None:
        self._trigger_timer.stop()
        self._external_timer.stop()
        self._daemon_timer.stop()

    # ------------------------------------------------------------------
    # 外部改动 / 守护进程状态探测
    # ------------------------------------------------------------------
    def poll_external_changes(self) -> bool:
        """磁盘 tasks.json 被外部改动时三方合并进内存，并通知界面刷新。

        返回是否发生了合并/吸收。合并不会丢任何一方的任务（见 store._merge_locked）。
        另外兼顾 `store.save()` 里已经吸收过的改动（改主题一类的保存会把外部任务
        吸进内存，此时磁盘指纹已对齐、`reload_if_changed()` 返回 False，
        但界面列表仍是陈旧快照，必须补一次刷新）。
        """
        changed = False
        try:
            reloaded = bool(self.store.reload_if_changed())
            # 两个都要求值（不能用 or 短路）：否则标记残留会让下一次轮询多刷一遍
            absorbed = bool(self.store.take_pending_absorb())
            changed = reloaded or absorbed
        except Exception:  # noqa: BLE001 - 探测失败不能拖垮界面定时器
            log.exception("探测外部改动失败")
            return False

        if not changed:
            return False
        summary = {
            "tasks": len(self.store.list_tasks()),
            "at": datetime.now().isoformat(timespec="seconds"),
        }
        log.info("已吸收外部配置改动（%d 个任务）", summary["tasks"])
        self.external_reloaded.emit(summary)
        self.tasks_changed.emit()
        for t in self.store.list_tasks():
            self.task_updated.emit(t.id)
        return True


    def poll_daemon_status(self) -> bool:
        """后台查询守护进程状态（IPC 阻塞，不能占 UI 线程）。

        结果缓存在 self.daemon_info 并发 daemon_state_changed 信号。
        返回是否已发起一次探测。
        """
        if not self._daemon_probe_busy.acquire(blocking=False):
            return False  # 上一次探测还没回来

        def _probe() -> None:
            try:
                from ..daemon import daemon_status

                info = daemon_status(timeout_ms=600)
            except Exception as exc:  # noqa: BLE001
                info = {"running": False, "error": str(exc)}
            finally:
                self._daemon_probe_busy.release()
            self.daemon_info = info
            try:
                self.daemon_state_changed.emit(info)
            except RuntimeError:  # 界面已销毁
                pass

        threading.Thread(target=_probe, daemon=True, name="bs-daemon-probe").start()
        return True

    def daemon_running(self) -> bool:
        """最近一次探测到的守护进程状态（未探测过则为 False）。"""
        return bool(self.daemon_info.get("running"))

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
        """读取历史记录（按文件指纹记忆化）。

        缺陷背景（实测）：运行记录页每次 ``on_shown`` 都重新读最多 2 MB 的
        ``history.jsonl`` 并逐行 ``json.loads``，阻塞 UI 线程 195–234 ms。

        缓存键是 ``(mtime_ns, size)``。历史文件是**追加**写入
        （``engine.append_history``），任何一次新备份都会让 size 与 mtime 变化，
        因此缓存能正确失效；而单纯切页时文件不变，直接命中。
        轮转（``_rotate_history_if_needed``）会让 size 变小，键同样变化。
        """
        from ..engine import history_path

        try:
            st = history_path().stat()
            key = (st.st_mtime_ns, st.st_size)
        except OSError:
            return read_history(300)
        cached = self._history_cache
        if cached is not None and cached[0] == key:
            return cached[1]
        rows = read_history(300)
        self._history_cache = (key, rows)
        return rows


def config_directory() -> Path:
    return config_dir()
