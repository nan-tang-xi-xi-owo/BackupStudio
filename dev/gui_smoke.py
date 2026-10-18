"""GUI 冒烟测试 + 截图（在离屏模式下运行，不弹窗）。"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

CONFIG = Path(tempfile.mkdtemp(prefix="bs-gui-cfg-"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CONFIG)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from backupstudio.config import (  # noqa: E402
    ArchiveOptions,
    BackupTask,
    RetentionPolicy,
    ScheduleConfig,
    SourceConfig,
)
from backupstudio.gui import stylesheet  # noqa: E402
from backupstudio.gui.appstate import AppState  # noqa: E402
from backupstudio.gui.main_window import MainWindow  # noqa: E402
from backupstudio.store import TaskStore  # noqa: E402


def seed(store: TaskStore) -> None:
    data = CONFIG / "demo-source"
    data.mkdir(exist_ok=True)
    (data / "readme.txt").write_text("demo", encoding="utf-8")
    tasks = [
        BackupTask(
            name="工作文档", sources=[SourceConfig(str(data))], destination=str(CONFIG / "backups"),
            options=ArchiveOptions(fmt="7z", password="secret", multivolume="7z-split", volume_value=100),
            schedule=ScheduleConfig(type="daily", time="02:00"),
            retention=RetentionPolicy(enabled=True, keep_last=14),
            last_status="success", last_message="完成：128 个文件",
            last_run=datetime.now().astimezone().isoformat(timespec="seconds"),
            last_duration=42.5,
        ),
        BackupTask(
            name="照片库", sources=[SourceConfig(str(data))], destination=str(CONFIG / "photo-backups"),
            options=ArchiveOptions(fmt="zip"), schedule=ScheduleConfig(type="weekly", time="03:30"),
            last_status="failure", last_message="7-Zip 退出码 2", 
        ),
        BackupTask(
            name="服务器配置", sources=[SourceConfig(str(data))], destination=str(CONFIG / "cfg"),
            options=ArchiveOptions(fmt="tar.gz"), schedule=ScheduleConfig(type="manual"),
        ),
    ]
    for t in tasks:
        store.upsert(t)
    store.save()


def main() -> int:
    out = ROOT / "build" / "shots"
    out.mkdir(parents=True, exist_ok=True)
    store = TaskStore(CONFIG / "tasks.json")
    seed(store)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    # 离屏模式没有字体数据库，手动加载系统中文字体以便截图
    from PySide6.QtGui import QFont, QFontDatabase

    if QFontDatabase.families() == []:
        added = False
        for path in (
            r"C:\Windows\Fonts\msyh.ttc",
            r"C:\Windows\Fonts\simhei.ttf",
            r"C:\Windows\Fonts\segoeui.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        ):
            if Path(path).exists():
                fid = QFontDatabase.addApplicationFont(path)
                if fid >= 0:
                    fams = QFontDatabase.applicationFontFamilies(fid)
                    if fams:
                        app.setFont(QFont(fams[0], 10))
                        added = True
        print("manual fonts:", added, QFontDatabase.families()[:4])
    state = AppState(store)
    app.setStyleSheet(stylesheet(state.palette))
    win = MainWindow(state)
    win.show()
    app.processEvents()

    errors: list[str] = []

    def shot(name: str, page: str) -> None:
        win._select(page)
        for _ in range(6):
            app.processEvents()
        win.grab().save(str(out / f"{name}.png"))
        print("saved", name)

    def open_dialog() -> None:
        from backupstudio.gui.task_dialog import TaskDialog

        dlg = TaskDialog(state, state.tasks()[0], parent=win)
        dlg.show()
        for _ in range(6):
            app.processEvents()
        dlg.grab().save(str(out / "dialog-task.png"))
        print("saved dialog-task")
        dlg.tabs.setCurrentIndex(1)
        for _ in range(4):
            app.processEvents()
        dlg.grab().save(str(out / "dialog-compress.png"))
        dlg.tabs.setCurrentIndex(2)
        for _ in range(4):
            app.processEvents()
        dlg.grab().save(str(out / "dialog-schedule.png"))
        dlg.close()

    def dark_shot() -> None:
        state.set_theme("dark")
        for _ in range(4):
            app.processEvents()
        win._select("tasks")
        for _ in range(4):
            app.processEvents()
        win.grab().save(str(out / "dark-tasks.png"))
        print("saved dark-tasks")
        state.set_theme("light")

    steps = [lambda: shot("tasks", "tasks"), lambda: shot("history", "history"),
             lambda: shot("env", "env"), lambda: shot("settings", "settings"),
             open_dialog, dark_shot]

    def run_step(i: int = 0) -> None:
        if i >= len(steps):
            app.quit()
            return
        try:
            steps[i]()
        except Exception as exc:  # noqa: BLE001
            import traceback

            errors.append(f"{exc}")
            traceback.print_exc()
        QTimer.singleShot(150, lambda: run_step(i + 1))

    QTimer.singleShot(300, run_step)
    QTimer.singleShot(20000, app.quit)
    app.exec()
    shutil.rmtree(CONFIG, ignore_errors=True)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
