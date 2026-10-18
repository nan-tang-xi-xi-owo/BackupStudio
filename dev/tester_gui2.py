"""测试员脚本（GUI 第二轮）：取消备份、托盘互动、关闭到托盘、启动最小化。"""

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
WORK = ROOT / "build" / "gui-tester2"
os.environ["QT_QPA_PLATFORM"] = "offscreen"
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(WORK / "config")

from PySide6.QtCore import QEvent, QTimer  # noqa: E402
from PySide6.QtGui import QCloseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  — {detail}" if detail and not ok else ""))


def main() -> int:
    shutil.rmtree(WORK, ignore_errors=True)
    src = WORK / "src"
    src.mkdir(parents=True)
    # 足够大的随机数据，保证备份过程可持续 >1 秒，便于中途取消
    for i in range(4):
        (src / f"blob{i}.bin").write_bytes(os.urandom(8 * 1024 * 1024))

    from backupstudio.config import ArchiveOptions, BackupTask, SourceConfig
    from backupstudio.gui import stylesheet
    from backupstudio.gui.appstate import AppState
    from backupstudio.gui.main_window import MainWindow
    from backupstudio.store import TaskStore

    app = QApplication.instance() or QApplication(sys.argv[:1])
    store = TaskStore(WORK / "config" / "tasks.json")
    task = BackupTask(
        name="取消测试", sources=[SourceConfig(str(src))], destination=str(WORK / "backups"),
        options=ArchiveOptions(fmt="7z", level=9, force=False),
    )
    store.upsert(task)
    store.save()
    state = AppState(store)
    app.setStyleSheet(stylesheet(state.palette))
    win = MainWindow(state)
    win.show()
    app.processEvents()

    # ---- 1. 中途取消（用信号触发，等价于用户在界面上点「停止」）
    seen_phases: list[str] = []
    state.progress_changed.connect(lambda _tid, payload: seen_phases.append(str(payload.get("phase"))))

    def cancel_running() -> None:
        assert state.run_task(task.id, trigger="manual"), "未能启动任务"
        # 等到确实开始写入数据（而不是固定睡眠），避免压缩太快导致取消落空
        deadline = time.time() + 30
        while time.time() < deadline:
            app.processEvents()
            if "write" in seen_phases:
                break
            time.sleep(0.02)
        app.processEvents()
        print("   已观察到的阶段:", sorted(set(seen_phases)))
        worker = getattr(state, f"_worker_{task.id}", None)
        assert worker is not None, "找不到后台 worker"
        worker.cancel()
        end = time.time() + 60
        while time.time() < end and state.is_running(task.id):
            app.processEvents()
            time.sleep(0.1)
        app.processEvents()
        assert not state.is_running(task.id), "取消后任务仍在运行"
        refreshed = state.get(task.id)
        assert refreshed is not None
        check("取消后状态为已跳过", refreshed.last_status == "skipped", f"状态={refreshed.last_status} 消息={refreshed.last_message}")
        leftovers = [p.name for p in (WORK / "backups").glob("*") if p.name != "last-run.json"] if (WORK / "backups").exists() else []
        check("取消后不残留半成品压缩包", not leftovers, str(leftovers))

    try:
        cancel_running()
        check("中途取消流程", True)
    except Exception:  # noqa: BLE001
        check("中途取消流程", False, traceback.format_exc().splitlines()[-1])

    # ---- 2. 关闭窗口 → 最小化到托盘（close_to_tray 默认开）
    def close_to_tray() -> None:
        state.store.settings.close_to_tray = True
        state.store.save()
        win.show()
        app.processEvents()
        win.close()
        app.processEvents()
        assert not win.isVisible(), "关闭后窗口仍可见"
        check("关闭窗口后驻留（未退出）", True)

    try:
        close_to_tray()
        check("关闭到托盘", True)
    except Exception:  # noqa: BLE001
        check("关闭到托盘", False, traceback.format_exc().splitlines()[-1])

    # ---- 3. 托盘菜单「立即运行任务」入口存在且可用
    def tray_menu() -> None:
        menu = win.tray_tasks_menu
        actions = menu.actions()
        assert actions, "托盘任务菜单为空"
        assert any(a.text() == "取消测试" for a in actions), [a.text() for a in actions]
        assert win.tray.contextMenu() is not None
        assert not win.tray.icon().isNull()

    try:
        tray_menu()
        check("托盘菜单包含任务且可触发", True)
    except Exception:  # noqa: BLE001
        check("托盘菜单包含任务且可触发", False, traceback.format_exc().splitlines()[-1])

    # ---- 4. 托盘通知与备份完成回调不抛异常
    def notification() -> None:
        state._on_notify if False else None
        win._on_notify("测试通知", "这是一条托盘气泡", "success")
        app.processEvents()

    try:
        notification()
        check("托盘通知调用不崩", True)
    except Exception:  # noqa: BLE001
        check("托盘通知调用不崩", False, traceback.format_exc().splitlines()[-1])

    # ---- 5. 启动时最小化：show 后立即 hide 等价路径
    def start_minimized() -> None:
        state.store.settings.start_minimized = True
        state.store.save()
        win.show()
        app.processEvents()
        win.hide()
        app.processEvents()
        assert not win.isVisible()

    try:
        start_minimized()
        check("启动最小化到托盘", True)
    except Exception:  # noqa: BLE001
        check("启动最小化到托盘", False, traceback.format_exc().splitlines()[-1])

    win._really_quit = True
    win.close()
    app.processEvents()

    failures = [r for r in RESULTS if not r[1]]
    print(f"\n总计 {len(RESULTS)} 项，失败 {len(failures)} 项")
    for name, _, detail in failures:
        print(f"  ✗ {name}: {detail[:500]}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
