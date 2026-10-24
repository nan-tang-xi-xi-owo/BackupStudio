"""双写者（GUI × CLI/守护进程）合并回归 —— 审查 W-A7-2（S1）/ R3-C12 / S3-G5-G6。

复现的缺陷链（W-A7 exp4 §B 实测）：
GUI 打开且有任务 A → CLI/守护进程往 tasks.json 加任务 B → GUI 不重读 →
GUI 任意一次保存（改主题就够）以内存全量覆写磁盘 → **B 静默消失**。

修复：TaskStore 记住载入时的磁盘基线 + (mtime,size) 指纹；
`save()` 前检测外部改动并做三方合并；`reload_if_changed()` 供界面/守护进程定时吸收。
保守方向：绝不丢任何一方的任务。

"两进程模拟"用同一文件上的两个 TaskStore 实例表达（各自独立 _load），
与真双进程在文件层看到的状态一致。
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backupstudio.config import BackupTask, RunRecord, ScheduleConfig, save_json_atomic  # noqa: E402
from backupstudio.store import TaskStore  # noqa: E402


def _task(name: str, **kw) -> BackupTask:
    t = BackupTask(name=name, schedule=ScheduleConfig(type="manual"), **kw)
    return t


def _touch_soon(path: Path) -> None:
    """确保 mtime 指纹一定变化（某些文件系统 mtime 粒度较粗）。"""
    try:
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 2.0))
    except OSError:
        pass


def _disk_names(path: Path) -> list[str]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [t["name"] for t in data.get("tasks", [])]


class TestExternalChangeDetection:
    def test_no_change_means_no_reload(self, tmp_path):
        path = tmp_path / "tasks.json"
        a = TaskStore(path)
        a.upsert(_task("A"))
        a.save()
        assert a.external_changed() is False
        assert a.reload_if_changed() is False

    def test_own_save_is_not_external_change(self, tmp_path):
        """自己写盘不应被判定为"外部改动"（否则每次保存都要多读一遍磁盘）。"""
        path = tmp_path / "tasks.json"
        a = TaskStore(path)
        a.upsert(_task("A"))
        a.save()
        a.upsert(_task("B"))
        a.save()
        assert a.external_changed() is False

    def test_missing_file_then_created_is_external(self, tmp_path):
        path = tmp_path / "tasks.json"
        a = TaskStore(path)  # 文件不存在
        a.upsert(_task("A"))
        a.save()
        # 另一个 store 覆写（模拟外部）
        b = TaskStore(path)
        b.upsert(_task("外部"))
        b.save()
        _touch_soon(path)
        assert a.external_changed() is True


class TestW_A7_2_LostTask:
    def test_gui_save_keeps_externally_added_task(self, tmp_path):
        """核心验收（W-A7 exp4 §B）：A 载入 → B 写新任务 → A save → 磁盘仍有 B 的任务。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("GUI原有任务"))
        gui.save()

        cli = TaskStore(path)
        cli.upsert(_task("CLI新增任务"))
        cli.save()
        assert set(_disk_names(path)) == {"GUI原有任务", "CLI新增任务"}

        # GUI 只是改了主题（与备份毫无关系的一次保存）
        gui.settings.theme_mode = "dark"
        gui.save()

        assert "CLI新增任务" in _disk_names(path), "GUI 保存抹掉了外部新增任务（W-A7-2 S1）"
        assert "GUI原有任务" in _disk_names(path)
        after = TaskStore(path)
        assert after.settings.theme_mode == "dark", "本地设置改动仍要生效"

    def test_save_merges_into_memory_so_signal_list_is_correct(self, tmp_path):
        """保存后内存也要看到外部任务（界面列表不能继续用陈旧快照）。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("GUI原有任务"))
        gui.save()
        other = TaskStore(path)
        other.upsert(_task("外部任务"))
        other.save()
        _touch_soon(path)

        gui.save()
        assert {t.name for t in gui.list_tasks()} == {"GUI原有任务", "外部任务"}

    def test_reload_if_changed_absorbs_and_reports(self, tmp_path):
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("本地"))
        gui.save()
        other = TaskStore(path)
        other.upsert(_task("外部"))
        other.save()
        _touch_soon(path)

        assert gui.reload_if_changed() is True
        assert "外部" in {t.name for t in gui.list_tasks()}
        assert gui.reload_if_changed() is False, "已吸收过就不应重复报告"

    def test_externally_edited_field_is_visible_after_save(self, tmp_path):
        """外部改写任务字段（如守护进程回写 last_status）→ GUI 保存不得回退它。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        t = _task("计划任务")
        gui.upsert(t)
        gui.save()

        daemon = TaskStore(path)
        dt = daemon.get(t.id)
        dt.last_status = "failure"
        dt.last_run = datetime.now().astimezone().isoformat(timespec="seconds")
        dt.last_message = "外部发现失败"
        daemon.upsert(dt)
        daemon.save()
        _touch_soon(path)

        gui.save()
        reloaded = TaskStore(path).get(t.id)
        assert reloaded.last_status == "failure", "GUI 保存把外部回写的运行状态洗掉了"
        assert reloaded.last_message == "外部发现失败"


class TestDeletionSemantics:
    def test_local_delete_survives_concurrent_external_edit(self, tmp_path):
        """本地删除是用户意图：同一次保存里外部并发改别的任务，也不把被删任务带回来。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        keep = _task("留下")
        gone = _task("删掉")
        gui.upsert(keep)
        gui.upsert(gone)
        gui.save()

        other = TaskStore(path)
        ok = other.get(keep.id)
        ok.destination = "D:/外部改过"
        other.upsert(ok)
        other.save()  # 外部磁盘内容仍含 "删掉"
        _touch_soon(path)

        assert gui.delete(gone.id) is True
        gui.save()
        names = _disk_names(path)
        assert "删掉" not in names, "本地删除被外部快照复活"
        assert "留下" in names
        assert TaskStore(path).get(keep.id).destination == "D:/外部改过", "外部改动也要吸收"

    def test_external_delete_absorbed_when_local_untouched(self, tmp_path):
        """外部删了一个任务、本地没碰过它 → 本地重载时吸收删除（不复活）。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        a = _task("任务A")
        gui.upsert(a)
        gui.upsert(_task("任务B"))
        gui.save()

        other = TaskStore(path)
        other.delete(a.id)
        other.save()
        _touch_soon(path)

        assert gui.reload_if_changed() is True
        assert "任务A" not in {t.name for t in gui.list_tasks()}
        assert "任务B" in {t.name for t in gui.list_tasks()}

    def test_local_edit_vs_external_delete_conflict_keeps_task(self, tmp_path):
        """删除 vs 本地修改冲突：宁多勿少，保留任务（外部可能是陈旧快照整体覆写）。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        a = _task("任务A")
        gui.upsert(a)
        gui.save()

        other = TaskStore(path)
        other.delete(a.id)
        other.save()
        _touch_soon(path)

        gui.get(a.id).destination = "D:/已改本地"
        gui.save()
        assert "任务A" in _disk_names(path), "本地已修改的任务不应被外部删除静默吃掉"
        assert TaskStore(path).get(a.id).destination == "D:/已改本地"

    def test_import_replace_does_not_resurrect_replaced_tasks(self, tmp_path):
        """导入"整体替换"是显式删除意图：合并不得把被替换掉的旧任务吸回来。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("旧任务"))
        gui.save()

        bundle = tmp_path / "bundle.json"
        src = TaskStore(tmp_path / "src.json")
        src.upsert(_task("导入任务"))
        src.export_bundle(bundle)

        # 外部（GUI 打开期间）往磁盘加了任务
        other = TaskStore(path)
        other.upsert(_task("外部任务"))
        other.save()
        _touch_soon(path)

        assert gui.import_bundle(bundle, replace=True) == 1
        names = _disk_names(path)
        assert "导入任务" in names
        assert "旧任务" not in names, "整体替换后被删的任务被合并复活"
        assert "外部任务" in names, "外部新增仍要保留（宁多勿少）"


class TestConcurrentFieldMerge:
    def test_two_sides_edit_different_fields(self, tmp_path):
        """双方各改不同字段（外部改 last_status，本地改目的地）→ 都不丢。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        t = _task("双改任务")
        gui.upsert(t)
        gui.save()
        tid = t.id

        other = TaskStore(path)
        ot = other.get(tid)
        ot.last_status = "success"
        ot.last_archive = "out.zip"
        other.upsert(ot)
        other.save()
        _touch_soon(path)

        gt = gui.get(tid)
        gt.destination = "E:/新位置"
        gt.enabled = False
        gui.upsert(gt)
        gui.save()

        final = TaskStore(path).get(tid)
        assert final.last_status == "success" and final.last_archive == "out.zip"
        assert final.destination == "E:/新位置" and final.enabled is False

    def test_newer_updated_at_wins_for_same_field(self, tmp_path):
        """同一字段双方都改：updated_at 新的一方胜出。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        t = _task("冲突任务", destination="C:/base")
        gui.upsert(t)
        gui.save()
        tid = t.id

        other = TaskStore(path)
        ot = other.get(tid)
        ot.destination = "D:/external"
        ot.updated_at = (datetime.now().astimezone() + timedelta(seconds=5)).isoformat(timespec="seconds")
        other.upsert(ot)
        # upsert 会把 updated_at 重置为 now，这里显式改回"更晚"
        other.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw["tasks"]:
            if item["id"] == tid:
                item["updated_at"] = (datetime.now().astimezone() + timedelta(seconds=5)).isoformat(timespec="seconds")
        save_json_atomic(path, raw)
        _touch_soon(path)

        gt = gui.get(tid)
        gt.destination = "E:/local"
        gt.updated_at = (datetime.now().astimezone() - timedelta(seconds=5)).isoformat(timespec="seconds")
        gui.tasks[tid] = gt
        gui.save()

        final = TaskStore(path).get(tid)
        assert final.destination == "D:/external", "外部 updated_at 更新时应胜出"

    def test_both_sides_add_different_tasks_all_survive(self, tmp_path):
        """双向各自新增：A 加 X、B 加 Y，两边交替保存后 X 与 Y 都在（宁多勿少）。"""
        path = tmp_path / "tasks.json"
        a = TaskStore(path)
        a.upsert(_task("共有"))
        a.save()
        b = TaskStore(path)

        a.upsert(_task("A新增"))
        a.save()
        b.upsert(_task("B新增"))
        b.save()

        names = set(_disk_names(path))
        assert {"共有", "A新增", "B新增"} <= names, names

    def test_repeated_saves_are_idempotent(self, tmp_path):
        """无外部改动时反复保存不产生任务增减（合并层不引入抖动）。"""
        path = tmp_path / "tasks.json"
        a = TaskStore(path)
        a.upsert(_task("T"))
        a.save()
        before = json.loads(path.read_text(encoding="utf-8"))["tasks"]
        for _ in range(3):
            a.save()
        after = json.loads(path.read_text(encoding="utf-8"))["tasks"]
        assert before == after

    def test_corrupt_disk_task_does_not_break_save(self, tmp_path):

        """磁盘出现畸形条目（非 dict / 无法解析）→ 保存不得因此崩溃，本地任务保住。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("好任务"))
        gui.save()

        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["tasks"].append("garbage-string")
        raw["tasks"].append({"id": "x", "schedule": 123})
        save_json_atomic(path, raw)
        _touch_soon(path)

        gui.settings.theme_mode = "dark"
        gui.save()
        assert "好任务" in _disk_names(path)

    def test_disk_replaced_with_non_dict_keeps_local_tasks(self, tmp_path):
        """磁盘被同步工具写成非字典（如 `[]`）→ 不据此判定"外部删光了任务"。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("重要任务"))
        gui.save()

        path.write_text("[]", encoding="utf-8")
        _touch_soon(path)

        gui.save()
        assert "重要任务" in _disk_names(path), "磁盘内容不可解释时不得丢本地任务"


class TestMalformedConfigStillLoads:
    """W-A5-7（S2）：settings 字段类型畸形曾让 GUI 与守护进程双双启动即崩。"""

    @pytest.mark.parametrize("bad", ["str", 123, ["a"], True])
    def test_bad_settings_does_not_crash_load(self, tmp_path, bad):
        path = tmp_path / "tasks.json"
        seed = TaskStore(path)
        seed.upsert(_task("幸存任务"))
        seed.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["settings"] = bad
        save_json_atomic(path, raw)

        store = TaskStore(path)  # 修复前：AttributeError 直接崩
        assert [t.name for t in store.list_tasks()] == ["幸存任务"], "任务列表必须优先于设置"
        assert store.settings.theme_mode == "system"

    def test_bad_notifications_section_falls_back(self, tmp_path):
        path = tmp_path / "tasks.json"
        seed = TaskStore(path)
        seed.upsert(_task("幸存任务"))
        seed.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["settings"] = {"notifications": "on", "theme_mode": "dark"}
        save_json_atomic(path, raw)

        store = TaskStore(path)
        assert [t.name for t in store.list_tasks()] == ["幸存任务"]

    def test_tasks_not_a_list_is_ignored(self, tmp_path):
        path = tmp_path / "tasks.json"
        save_json_atomic(path, {"app": "x", "settings": {}, "tasks": {"a": 1}})
        store = TaskStore(path)
        assert store.list_tasks() == []

    def test_save_after_bad_settings_keeps_tasks(self, tmp_path):
        """崩溃防住之后还要能正常保存（外部改动照样合并）。"""
        path = tmp_path / "tasks.json"
        seed = TaskStore(path)
        seed.upsert(_task("任务1"))
        seed.save()
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["settings"] = 123
        save_json_atomic(path, raw)

        gui = TaskStore(path)
        other = TaskStore(path)
        other.upsert(_task("外部任务"))
        other.save()
        _touch_soon(path)
        gui.save()
        names = _disk_names(path)
        assert "任务1" in names and "外部任务" in names



class TestDaemonWritebackStillWorks:
    def test_daemon_style_writeback_survives_gui_theme_save(self, tmp_path):
        """W-A5 质疑②附带（C12 方向 2）：守护进程回写 last_run 后 GUI 改主题不得回退它。"""
        from backupstudio.daemon import _apply_record

        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        t = _task("每日任务")
        t.schedule = ScheduleConfig(type="daily", time="02:00")
        gui.upsert(t)
        gui.save()

        # 守护进程：fresh load → 回写 → save
        daemon = TaskStore(path)
        dt = daemon.get(t.id)
        rec = RunRecord(
            task_id=dt.id,
            task_name=dt.name,
            started_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            status="success",
            archive="a.zip",
            duration=1.0,
        )
        _apply_record(dt, rec, "完成")
        daemon.upsert(dt)
        daemon.save()
        _touch_soon(path)

        gui.settings.theme_mode = "dark"
        gui.save()

        final = TaskStore(path).get(t.id)
        assert final.last_run == rec.started_at, "last_run 被 GUI 洗回空 → 同一天重复备份链"
        assert final.last_status == "success"

    def test_undo_save_keeps_external_task(self, tmp_path):
        """撤销一次保存同样是覆写：合并后外部任务不丢。"""
        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("老任务"))
        gui.save()
        gui.upsert(_task("新任务"))
        gui.save()

        other = TaskStore(path)
        other.upsert(_task("外部任务"))
        other.save()
        _touch_soon(path)

        assert gui.undo_last_save() is True
        names = _disk_names(path)
        assert "外部任务" in names, "撤销保存抹掉了外部任务"
        assert "老任务" in names


class TestAppStateReloadWiring:
    """AppState 的外部改动探测器（offscreen，无事件循环：直接调方法）。"""

    @staticmethod
    def _qapp():
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PySide6.QtWidgets import QApplication

        return QApplication.instance() or QApplication([])

    def test_poll_emits_tasks_changed(self, tmp_path, monkeypatch):
        self._qapp()
        from PySide6.QtCore import QTimer

        from backupstudio.gui.appstate import AppState

        monkeypatch.setattr(QTimer, "start", lambda self, *a: None)
        monkeypatch.setattr(QTimer, "stop", lambda self: None)

        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("本地"))
        gui.save()
        state = AppState(gui)
        seen = []
        state.tasks_changed.connect(lambda: seen.append("tasks_changed"))
        state.external_reloaded.connect(lambda info: seen.append(("reloaded", info["tasks"])))

        other = TaskStore(path)
        other.upsert(_task("外部新增"))
        other.save()
        _touch_soon(path)

        assert state.poll_external_changes() is True
        assert "外部新增" in {t.name for t in state.tasks()}
        assert "tasks_changed" in seen
        assert any(isinstance(s, tuple) and s[0] == "reloaded" for s in seen)
        assert state.poll_external_changes() is False, "无新改动时不再发信号"

    def test_poll_refreshes_after_save_absorbed_external(self, tmp_path, monkeypatch):
        """外部改动在 GUI 保存时被吸收（磁盘指纹随即对齐）→ 下一次轮询仍要刷新列表。"""
        self._qapp()
        from PySide6.QtCore import QTimer

        from backupstudio.gui.appstate import AppState

        monkeypatch.setattr(QTimer, "start", lambda self, *a: None)
        monkeypatch.setattr(QTimer, "stop", lambda self: None)

        path = tmp_path / "tasks.json"
        gui = TaskStore(path)
        gui.upsert(_task("本地"))
        gui.save()
        state = AppState(gui)
        seen = []
        state.tasks_changed.connect(lambda: seen.append(1))

        other = TaskStore(path)
        other.upsert(_task("外部新增"))
        other.save()
        _touch_soon(path)

        gui.settings.theme_mode = "dark"
        gui.save()  # 保存时合并吸收了外部任务，磁盘指纹已对齐
        assert "外部新增" in {t.name for t in gui.list_tasks()}
        assert state.poll_external_changes() is True, "保存吸收的外部任务应触发一次列表刷新"
        assert seen

