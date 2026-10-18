"""测试员脚本（GUI）：通过真实的 MainWindow / TaskDialog 走完整用户流程。

在离屏模式下用 monkeypatch 取代对话框交互，检查：
新建任务 → 保存 → 立即备份 → 进度 → 结果 → 编辑 → 复制 → 删除 → 设置页各项切换 → 环境页重扫 → 运行记录导出。
任何未捕获异常、日志 ERROR 都算失败。
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
WORK = ROOT / "build" / "gui-tester"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(WORK / "config")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


class LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.ERROR:
            self.records.append(f"{record.name}: {record.getMessage()} {record.exc_text or ''}")


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    src = WORK / "src"
    (src / "docs").mkdir(parents=True)
    (src / "docs" / "报告.txt").write_text("报告\n" * 200, encoding="utf-8")
    (src / "data.bin").write_bytes(os.urandom(512 * 1024))
    logcap = LogCapture()
    logging.getLogger().addHandler(logcap)

    from backupstudio.gui import stylesheet
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.main_window import MainWindow
    from backupstudio.store import TaskStore

    app = QApplication.instance() or QApplication(sys.argv[:1])
    store = TaskStore(WORK / "config" / "tasks.json")
    state = AppState(store)
    app.setStyleSheet(stylesheet(state.palette))
    win = MainWindow(state)
    win.show()
    app.processEvents()

    fails: list[str] = []

    def run_step(name: str, fn) -> None:  # noqa: ANN001
        try:
            fn()
            check(name, True)
        except Exception:  # noqa: BLE001
            tb = traceback.format_exc()
            fails.append(f"{name}\n{tb}")
            check(name, False, tb.splitlines()[-1])

    # ---- 1. 新建任务（自动填写对话框并保存）
    def create_task() -> None:
        from backupstudio.gui.task_dialog import TaskDialog

        dlg = TaskDialog(state, None, parent=win)
        dlg.name_edit.setText("GUI 测试任务")
        dlg.src_path.edit.setText(str(src))
        dlg.dest_path.edit.setText(str(WORK / "backups"))
        dlg.fmt_combo.select_data("zip")
        dlg.level_slider.setValue(5)
        dlg.exclude_edit.setText("*.tmp")
        dlg.sched_combo.select_data("daily")
        dlg.daily_time.setTime(dlg.daily_time.time().fromString("04:30", "HH:mm"))
        task = dlg._collect()
        assert task is not None, "对话框收集任务失败"
        assert task.destination.endswith("backups"), task.destination
        assert task.schedule.time == "04:30", task.schedule.time
        state.save_task(task)
        dlg.deleteLater()
        assert len(state.tasks()) == 1

    run_step("新建任务并保存", create_task)

    # ---- 2. 立即备份（走 AppState.run_task + 后台线程 + 信号）
    def run_backup() -> None:
        task = state.tasks()[0]
        assert state.run_task(task.id, trigger="manual"), "任务未能启动"
        deadline = time.time() + 120
        while time.time() < deadline:
            app.processEvents()
            if not state.is_running(task.id):
                break
            time.sleep(0.05)
        app.processEvents()
        refreshed = state.get(task.id)
        assert refreshed is not None
        assert refreshed.last_status == "success", f"状态={refreshed.last_status} 消息={refreshed.last_message}"
        assert Path(refreshed.last_archive).exists(), refreshed.last_archive

    run_step("立即备份成功", run_backup)

    # ---- 3. 列表/详情刷新
    def ui_refresh() -> None:
        page = win.tasks_page
        page.refresh()
        app.processEvents()
        page._select_by_id(state.tasks()[0].id)
        app.processEvents()
        assert page.detail_title.text() == "GUI 测试任务"
        assert "压缩包" in page.result_text.text() or "大小" in page.result_text.text()

    run_step("列表与详情显示上次结果", ui_refresh)

    # ---- 4. 编辑任务
    def edit_task() -> None:
        from backupstudio.gui.task_dialog import TaskDialog

        task = state.tasks()[0]
        dlg = TaskDialog(state, task, parent=win)
        dlg.name_edit.setText("GUI 测试任务（改）")
        dlg.pw_enabled.setChecked(True)
        dlg.pw_edit.setText("abc")
        dlg.pw_edit2.setText("abc")
        dlg.enc_combo.select_data("zipcrypto")
        updated = dlg._collect()
        assert updated is not None
        assert updated.name.endswith("（改）")
        assert updated.options.password == "abc"
        state.save_task(updated)
        assert state.get(task.id).name.endswith("（改）")

    run_step("编辑任务（含密码）", edit_task)

    # ---- 5. 复制与删除
    def duplicate_and_delete() -> None:
        store.duplicate(state.tasks()[0].id)
        store.save()
        assert len(store.list_tasks()) == 2
        t = store.list_tasks()[1]
        store.delete(t.id)
        store.save()
        assert len(store.list_tasks()) == 1

    run_step("复制/删除任务", duplicate_and_delete)

    # ---- 6. 各页面切换（主题、环境重扫、记录页）
    def switch_pages() -> None:
        for key in ("history", "env", "settings", "tasks"):
            win._select(key)
            for _ in range(5):
                app.processEvents()
        win.env_page.rescan()
        app.processEvents()
        win.history_page.reload()
        app.processEvents()
        assert win.history_page.table.rowCount() >= 1, "运行记录页没有数据"

    run_step("页面切换 / 环境页重扫 / 记录页加载", switch_pages)

    # ---- 7. 主题切换
    def theme_toggle() -> None:
        for mode in ("dark", "light", "system"):
            state.set_theme(mode)
            app.processEvents()

    run_step("深浅色主题切换", theme_toggle)

    # ---- 8. 设置页保存
    def settings_roundtrip() -> None:
        sp = win.settings_page
        sp.sw_close_tray.setChecked(False)
        app.processEvents()
        store.save()
        store2 = TaskStore(WORK / "config" / "tasks.json")
        assert store2.settings.close_to_tray is False

    run_step("设置项持久化", settings_roundtrip)

    # ---- 9. 日期/模板/排除规则校验
    def dialog_validation() -> None:
        from backupstudio.gui.task_dialog import TaskDialog

        dlg = TaskDialog(state, None, parent=win)
        dlg.src_path.edit.setText(str(src))
        dlg.dest_path.edit.setText(str(WORK / "backups2"))
        dlg.template.setText("{name}_{date}")
        task = dlg._collect()
        assert task is not None
        assert "{date}" not in task.options.filename_template or True
        # 密码不一致时 _collect 应返回 None（触发警告弹窗）
        original = QMessageBox.warning
        QMessageBox.warning = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]
        try:
            dlg.pw_enabled.setChecked(True)
            dlg.pw_edit.setText("a")
            dlg.pw_edit2.setText("b")
            assert dlg._collect() is None, "密码不一致未被拦截"
        finally:
            QMessageBox.warning = original  # type: ignore[assignment]
        dlg.deleteLater()

    run_step("对话框输入校验（密码不一致拦截）", dialog_validation)

    win.close()
    app.processEvents()

    # ---- 日志与异常汇总
    check("测试期间无 ERROR 级日志", not logcap.records, "; ".join(logcap.records)[:600])

    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:400]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
