"""验证历史页分页 + 实测打开耗时（1.1.3）。"""
import os, sys, time
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
cfg = Path(os.environ["APPDATA"]) / "BackupStudio"

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow

st = AppState(TaskStore(cfg / "tasks.json"))
w = MainWindow(st); w.resize(1100, 700); w.show()
for _ in range(8):
    app.processEvents()

t = time.perf_counter()
hp = w._page("history")
for _ in range(6):
    app.processEvents()
print(f"  HistoryPage 构造:         {time.perf_counter() - t:6.3f}s")

t = time.perf_counter()
hp.on_shown()
for _ in range(6):
    app.processEvents()
d1 = time.perf_counter() - t
print(f"  首次 on_shown():          {d1:6.3f}s  行数={hp.table.rowCount()}  {hp.page_info.text()}")

t = time.perf_counter()
hp.on_shown()
for _ in range(6):
    app.processEvents()
print(f"  二次 on_shown():          {time.perf_counter() - t:6.3f}s")

t = time.perf_counter()
hp._show_more()
for _ in range(4):
    app.processEvents()
print(f"  显示更多(+200):           {time.perf_counter() - t:6.3f}s  行数={hp.table.rowCount()}")

t = time.perf_counter()
tp = w._page("tasks")
for _ in range(6):
    app.processEvents()
print(f"  TasksPage 构造:           {time.perf_counter() - t:6.3f}s")

t = time.perf_counter()
tp.on_shown()
for _ in range(6):
    app.processEvents()
print(f"  TasksPage.on_shown():     {time.perf_counter() - t:6.3f}s  列表项={tp.list.count()}")
w.close()
