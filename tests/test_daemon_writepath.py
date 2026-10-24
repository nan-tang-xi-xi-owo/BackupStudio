"""daemon 写回链路回归（E1 批次 0.8 / 审查 C2）。

覆盖：
1. 磁盘写回失败时内存 store 兜底更新 —— 3 个 tick 只执行 1 次（无 15s 重复触发链）；
2. 任务运行期间被删除 —— 不再抛 UnboundLocalError，正常走通知段；
3. 正常写回路径回归 —— 磁盘持久化仍更新。
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta

import pytest

from backupstudio import daemon as daemon_mod
from backupstudio.config import BackupTask, RunRecord, ScheduleConfig
from backupstudio.engine import ExecutionResult
from backupstudio.store import TaskStore


def _make_task(name: str = "写回测试") -> BackupTask:
    # last_run 设为 2 天前的 02:05：daily 02:00 在任何时刻运行都判定为到期，
    # 且不落入"首次启用宽限期"分支（避免依赖测试执行时辰）。
    old = (datetime.now() - timedelta(days=2)).replace(hour=2, minute=5, second=0, microsecond=0)
    return BackupTask(
        name=name,
        schedule=ScheduleConfig(type="daily", time="02:00"),
        last_run=old.isoformat(timespec="seconds"),
    )


def _make_result(task: BackupTask, status: str = "success", message: str = "完成") -> ExecutionResult:
    rec = RunRecord(
        task_id=task.id,
        task_name=task.name,
        started_at=datetime.now().isoformat(timespec="seconds"),
        status=status,
        archive=f"{task.name}.zip",
        duration=1.25,
        errors=["磁盘已满"] if status == "failure" else [],
    )
    return ExecutionResult(rec, message=message)


def _join_workers(timeout: float = 5.0) -> None:
    """等待 _tick 启动的 bs-task-* 后台执行线程结束。"""
    for th in threading.enumerate():
        if th.name.startswith("bs-task-") and th.is_alive():
            th.join(timeout)


@pytest.fixture()
def service():
    return daemon_mod.DaemonService()


@pytest.fixture()
def notifications(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(daemon_mod, "_native_notify", lambda *a, **k: calls.append((a, k)))
    return calls


class TestDaemonWriteback:
    def test_save_failure_ticks_run_once(self, service, notifications, monkeypatch):
        """验收（E1 0.8）：mock TaskStore.save 抛 OSError，3 个 tick 后 engine.run 只被调用 1 次。"""
        task = _make_task()
        original_last_run = task.last_run
        service.store.upsert(task)
        service.store.save()  # 真实写入磁盘（在 patch save 之前）

        calls: list[str] = []
        results: list[ExecutionResult] = []

        def fake_run(task_, trigger="manual", progress_cb=None, cancel=None, apply_cleanup=True):
            calls.append(task_.id)
            res = _make_result(task_, status="success")
            results.append(res)
            return res

        monkeypatch.setattr(service.engine, "run", fake_run)

        def boom_save(self):
            raise OSError("磁盘只读（模拟写回失败）")

        monkeypatch.setattr(TaskStore, "save", boom_save)

        for _ in range(3):
            service._tick()
            _join_workers()

        assert len(calls) == 1, "写回失败后内存 last_run 未生效，任务被重复触发（15s 重复执行链）"

        # 内存 store 已被兜底更新（_tick 的 due() 判定依据）
        mem = service.store.get(task.id)
        assert mem is not None
        assert mem.last_status == "success"
        assert mem.last_run == results[0].record.started_at
        assert mem.last_archive == results[0].record.archive

        # 磁盘确实未被写回（证明上面生效的是内存兜底，而非磁盘重载）
        on_disk = json.loads(service.store.path.read_text(encoding="utf-8"))
        disk_task = next(t for t in on_disk["tasks"] if t["id"] == task.id)
        assert disk_task["last_run"] == original_last_run

    def test_task_deleted_during_run(self, service, notifications, monkeypatch):
        """任务运行期间被删除（fresh.get 为 None）：不抛 UnboundLocalError，正常走通知段。"""
        task = _make_task()
        service.store.upsert(task)  # 仅在内存，磁盘上不存在（模拟运行中被删除）

        def fake_run(task_, trigger="manual", progress_cb=None, cancel=None, apply_cleanup=True):
            return _make_result(task_, status="failure")

        monkeypatch.setattr(service.engine, "run", fake_run)

        service._execute(task)  # 修复前此处抛 UnboundLocalError

        assert service.store.get(task.id) is None, "内存 store 中应已删除该任务"
        assert len(notifications) == 1, "失败通知应照常发出（正常走到通知段）"

    def test_success_path_persists(self, service, notifications, monkeypatch):
        """回归：写回成功时磁盘持久化更新。"""
        task = _make_task()
        service.store.upsert(task)
        service.store.save()

        holder: list[ExecutionResult] = []

        def fake_run(task_, trigger="manual", progress_cb=None, cancel=None, apply_cleanup=True):
            res = _make_result(task_, status="success")
            holder.append(res)
            return res

        monkeypatch.setattr(service.engine, "run", fake_run)

        service._execute(task)

        rec = holder[0].record
        persisted = TaskStore(service.store.path).get(task.id)
        assert persisted is not None
        assert persisted.last_status == "success"
        assert persisted.last_run == rec.started_at
        assert persisted.last_archive == rec.archive
        assert persisted.last_duration == rec.duration
        assert persisted.last_message == holder[0].message
        assert not notifications  # 成功不触发通知
