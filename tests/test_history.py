"""历史记录断档回归（审查 P0-16）：早退路径必须写 history。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _read_history():
    from backupstudio.engine import read_history

    return read_history(50)


class TestHistoryOnEarlyExit:
    def test_skipped_missing_source_writes_history(self, tmp_path):
        """来源不存在（skipped 早退）必须出现在 history 里。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        task = BackupTask(
            name="hs1",
            sources=[SourceConfig(path=str(tmp_path / "nonexistent"))],
            destination=str(tmp_path / "out"),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        rec = TaskEngine().run(task).record
        assert rec.status == "skipped"
        rows = [r for r in _read_history() if r.get("task_id") == task.id]
        assert rows, "skipped 早退没有写 history（回归！）"
        assert rows[-1]["status"] == "skipped"

    def test_cancelled_writes_history(self, tmp_path):
        """取消（ArchiveCancelled 早退）必须写 history，且不留半成品归档。

        确定性驱动（审查 R3-N3 复检）：原先依赖 progress_cb 翻转取消标志，
        但 Progress.emit 有 0.12s 节流——小数据在无 7-Zip 环境下整个写入
        只有十几毫秒，回调永远赶不上，取消标志直到收尾才被设置。
        改为按轮询次数驱动：引擎与后端在「逐条目 → 写入文件入口」会多次
        轮询 cancel，第 3 次返回 True 即"备份进行中点取消"，且在有无
        7-Zip 的环境下路径一致（两套后端的 add_file 入口都检查取消）。
        """
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        src = tmp_path / "data"
        src.mkdir()
        (src / "big.bin").write_bytes(b"x" * (4 * 1024 * 1024))
        task = BackupTask(
            name="hs2",
            sources=[SourceConfig(path=str(src))],
            destination=str(tmp_path / "out2"),
            options=ArchiveOptions(fmt="zip", force=True),
        )
        calls = {"n": 0}

        def cancel() -> bool:
            calls["n"] += 1
            return calls["n"] >= 3

        rec = TaskEngine().run(task, cancel=cancel).record
        assert calls["n"] >= 3, "取消回调从未被轮询（引擎取消链路断裂）"
        assert rec.status == "skipped"
        rows = [r for r in _read_history() if r.get("task_id") == task.id]
        assert rows, "取消早退没有写 history（回归！）"
        assert rows[-1]["status"] == "skipped"
        leftovers = list((tmp_path / "out2").glob("*.zip"))
        assert not leftovers, f"取消后残留半成品归档：{leftovers}"

    def test_backend_unavailable_writes_history(self, tmp_path, no_7z):
        """后端不可用（failure 早退）必须写 history。"""
        from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
        from backupstudio.engine import TaskEngine

        src = tmp_path / "d3"
        src.mkdir()
        (src / "a.txt").write_text("x", encoding="utf-8")
        task = BackupTask(
            name="hs3",
            sources=[SourceConfig(path=str(src))],
            destination=str(tmp_path / "out3"),
            options=ArchiveOptions(fmt="rar"),  # 无 rar 引擎 → BackendUnavailable
        )
        rec = TaskEngine().run(task).record
        assert rec.status == "failure"
        rows = [r for r in _read_history() if r.get("task_id") == task.id]
        assert rows, "backend 不可用早退没有写 history（回归！）"


class TestHistoryRotation:
    def test_rotation_keeps_recent_lines(self, tmp_path, monkeypatch):
        """轮转（审查 E1-1.7）：超限后只保留最近若干行，不再无限增长。"""
        import json

        from backupstudio import engine as eng

        monkeypatch.setattr(eng, "_HISTORY_MAX_BYTES", 2048)
        monkeypatch.setattr(eng, "_HISTORY_KEEP_LINES", 20)
        path = eng.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        # 先写一行旧记录，再写大量填充行把文件撑过上限
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"task_id": "old", "status": "success"}) + "\n")
            for i in range(200):
                fh.write(json.dumps({"task_id": f"fill{i}", "status": "success", "pad": "x" * 64}) + "\n")
        assert path.stat().st_size > 2048

        from backupstudio.config import RunRecord

        rec = RunRecord(task_id="newest", task_name="t", started_at="2026-10-21T00:00:00", status="success")
        eng.append_history(rec)
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert len(lines) <= 21, f"轮转后仍保留 {len(lines)} 行"
        assert json.loads(lines[-1])["task_id"] == "newest", "最新记录必须保留"
        assert all(json.loads(ln)["task_id"] != "old" for ln in lines), "最旧记录应被轮转掉"

    def test_no_rotation_below_limit(self, tmp_path, monkeypatch):
        """未超限时轮转不得动文件。"""
        import json

        from backupstudio import engine as eng

        path = eng.history_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(json.dumps({"task_id": "keep1", "status": "success"}) + "\n")
            fh.write(json.dumps({"task_id": "keep2", "status": "success"}) + "\n")

        from backupstudio.config import RunRecord

        eng.append_history(RunRecord(task_id="keep3", task_name="t", started_at="2026-10-21T00:00:00", status="success"))
        ids = [json.loads(ln)["task_id"] for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        assert ids == ["keep1", "keep2", "keep3"]
