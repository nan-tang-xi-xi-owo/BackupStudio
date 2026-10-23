"""daemon 写回链路与 IPC 健壮性回归（E1 批次 0.8 / 审查 C2 / 审查 W-A5-1）。

覆盖：
1. 磁盘写回失败时内存 store 兜底更新 —— 3 个 tick 只执行 1 次（无 15s 重复触发链）；
2. 任务运行期间被删除 —— 不再抛 UnboundLocalError，正常走通知段；
3. 正常写回路径回归 —— 磁盘持久化仍更新；
4. IPC 畸形载荷（合法 JSON 但不是对象：`[]` / `"hi"` / `123` / `null` / 垃圾字节）
   —— 必须得到结构化错误回复，且之后正常命令照旧应答（W-A5-1 S1：旧实现
   在此处 AttributeError → 该轮不回包 → REP 插座永久楔死，ping/stop 再无响应）；
5. tick 的重载改为三方合并 —— 外部新增任务被吸收、本地未落盘的兜底更新不被洗掉。
"""

from __future__ import annotations

import contextlib
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

    def test_tick_absorbs_external_task_keeps_memory_fallback(self, service, notifications):
        """tick 的重载已改为三方合并：外部新增任务被吸收，本地未落盘的兜底更新不被洗掉。

        （对应 test_save_failure_ticks_run_once 里"写回失败 → 只更新了内存"的那个状态：
        旧实现 _tick 一旦看到磁盘 mtime 变化就 `TaskStore(path)` 全量重建，把内存兜底洗掉，
        于是任务在下个 tick 又被判定到期 → 15s 重复执行链。）
        """
        task = _make_task(name="本地已跑")
        task.enabled = False  # 本用例只验合并语义，不让 tick 真跑备份
        service.store.upsert(task)
        service.store.save()

        # 模拟本地内存里有尚未落盘的改动（写回失败后的兜底更新）
        mem = service.store.get(task.id)
        mem.last_status = "success"
        mem.last_run = datetime.now().isoformat(timespec="seconds")
        mem.last_message = "磁盘只读时的内存兜底"

        # 外部进程（CLI/另一份界面）往磁盘加一个任务（禁调度，避免 tick 真跑备份）
        ext = TaskStore(service.store.path)
        added = _make_task(name="外部新增")
        added.enabled = False
        ext.upsert(added)
        ext.save()

        service._tick()
        _join_workers()

        names = {t.name for t in service.store.list_tasks()}
        assert "外部新增" in names, "外部新增任务未被吸收"
        kept = service.store.get(task.id)
        assert kept is not None
        assert kept.last_status == "success", "本地兜底的运行状态被外部快照洗掉"
        assert kept.last_message == "磁盘只读时的内存兜底"
        assert not notifications


# --------------------------------------------------------------------------
# IPC 健壮性（审查 W-A5-1，S1）
# --------------------------------------------------------------------------

# 合法 JSON 但不是对象 / 完全不是 JSON —— 旧实现在这些载荷上楔死 REP 插座
MALFORMED = [b"[]", b'"hi"', b"123", b"null", b"true", b"[1, 2]", b"not json", b"\xff\xfe"]


class TestIpcMalformed:
    def test_handle_rejects_non_dict_without_raising(self, service):
        """_handle 直接收到非 dict 也不抛（防御所有调用方）。"""
        for bad in ([], "hi", 123, None, True, {"cmd": 123}, {"cmd": ["run"]}):
            reply = service._handle(bad)
            assert isinstance(reply, dict) and reply.get("ok") is False, bad

    def test_reply_reports_error_for_malformed_payloads(self, service):
        """每个畸形载荷都必须拿到结构化错误回复（而不是异常或"什么都没有"）。"""
        for raw in MALFORMED:
            reply = service._reply(raw)
            assert isinstance(reply, dict), f"{raw!r} 未得到 dict 回复"
            assert reply.get("ok") is False, f"{raw!r} 未得到错误回复：{reply}"
            assert reply.get("error"), f"{raw!r} 缺少 error 说明"

    def test_valid_commands_still_answer(self, service):
        """畸形载荷之后，ping/status/reload/stop 与未知命令都照常应答。"""
        assert service._handle({"cmd": "ping"}).get("ok") is True
        assert service._handle({"cmd": "status"}).get("ok") is True
        assert service._handle({"cmd": "reload"}).get("ok") is True
        assert service._handle({"cmd": "nosuch"}).get("ok") is False
        assert service._handle({"cmd": "run", "task_id": "missing"}).get("ok") is False
        assert service._handle({"cmd": "run", "task_id": 123}).get("ok") is False
        assert service._handle({}).get("ok") is True  # 无 cmd → 默认 status

    def test_rep_socket_survives_malformed_burst(self, service):
        """端到端：真 REP 插座上连发畸形载荷，之后的 ping/stop 仍必须拿到应答。

        这就是 W-A5-1 的复现脚本：修复后 daemon 进程活着、IPC 也活着；
        修复前第一个畸形载荷之后所有请求都会超时（REP 卡在"已收未回"）。
        """
        zmq = pytest.importorskip("zmq")
        import socket as _socket
        import time as _time

        # 挑一个空闲端口，避免与真实守护进程抢占 49371
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        service.port = port
        service.endpoint = daemon_mod.ipc_endpoint(port)

        ctx, sock = daemon_mod._make_zmq_server(service.endpoint)
        loop = threading.Thread(target=service._ipc_loop, args=((ctx, sock),), daemon=True, name="bs-ipc-test")
        loop.start()
        client_ctx = zmq.Context()  # 独立上下文：服务端退出时的 ctx.term() 不会被本端拖住
        client = None
        try:
            client = client_ctx.socket(zmq.REQ)
            client.linger = 0
            client.setsockopt(zmq.RCVTIMEO, 3000)
            client.setsockopt(zmq.SNDTIMEO, 3000)
            client.connect(service.endpoint)
            for raw in MALFORMED:
                client.send(raw)
                got = json.loads(client.recv().decode("utf-8"))
                assert got.get("ok") is False, f"{raw!r} → {got}"
            # 畸形风暴之后仍然应答
            client.send(json.dumps({"cmd": "ping"}).encode())
            assert json.loads(client.recv().decode("utf-8")).get("ok") is True
            client.send(json.dumps({"cmd": "status"}).encode())
            assert json.loads(client.recv().decode("utf-8")).get("ok") is True
            # stop 也必须应答（否则 GUI 只能 6s 后硬杀 → PID/state.json 永久陈旧）
            client.send(json.dumps({"cmd": "stop"}).encode())
            assert json.loads(client.recv().decode("utf-8")).get("ok") is True
            deadline = _time.time() + 5
            while _time.time() < deadline and loop.is_alive():
                _time.sleep(0.05)
            assert not loop.is_alive(), "stop 应答后 IPC 线程未退出"
        finally:
            service._stop.set()
            if client is not None:
                with contextlib.suppress(Exception):
                    client.close(0)
            with contextlib.suppress(Exception):
                client_ctx.term()
            loop.join(5)

