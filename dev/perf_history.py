"""定位「运行记录」「备份任务」页卡顿（1.1.3）。

用户反馈：每次打开这两个页面都卡，怀疑历史记录太多。
先量真实数据量与各环节耗时，不猜。
"""
import json
import os
import sys
import time
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))

cfg = Path(os.environ["APPDATA"]) / "BackupStudio"
print(f"  配置: {cfg}")
for name in ("tasks.json", "history.jsonl", "state.json"):
    f = cfg / name
    if f.is_file():
        print(f"    {name}: {f.stat().st_size / 1024:.0f} KB")
hist = cfg / "history.jsonl"
if hist.is_file():
    n = sum(1 for _ in hist.open(encoding="utf-8", errors="replace"))
    print(f"    history 行数: {n}")

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore

t = time.perf_counter()
ts = TaskStore(cfg / "tasks.json")
print(f"\n  TaskStore 构造: {time.perf_counter() - t:.3f}s")

t = time.perf_counter()
h = ts.history()
print(f"  store.history() 首次: {time.perf_counter() - t:.3f}s  条数={len(h)}")

t = time.perf_counter()
h2 = ts.history()
print(f"  store.history() 二次: {time.perf_counter() - t:.3f}s  条数={len(h2)}")

from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow

st = AppState(ts)
w = MainWindow(st); w.resize(1100, 700); w.show()
for _ in range(8):
    app.processEvents()

# 运行记录页：构造 + 首次打开
t = time.perf_counter()
hp = w._page("history")
for _ in range(6):
    app.processEvents()
print(f"  HistoryPage 构造: {time.perf_counter() - t:.3f}s")

t = time.perf_counter()
hp.on_shown()
for _ in range(6):
    app.processEvents()
print(f"  HistoryPage.on_shown(): {time.perf_counter() - t:.3f}s  表格行数={hp.table.rowCount()}")

t = time.perf_counter()
hp.on_shown()
for _ in range(6):
    app.processEvents()
print(f"  第二次 on_shown(): {time.perf_counter() - t:.3f}s")

# 备份任务页
t = time.perf_counter()
tp = w._page("tasks")
for _ in range(6):
    app.processEvents()
print(f"  TasksPage 构造: {time.perf_counter() - t:.3f}s")
t = time.perf_counter()
tp.on_shown()
for _ in range(6):
    app.processEvents()
print(f"  TasksPage.on_shown(): {time.perf_counter() - t:.3f}s  列表项={tp.list.count()}")
w.close()
