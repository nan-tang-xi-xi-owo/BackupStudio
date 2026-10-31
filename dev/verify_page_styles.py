"""量化三个页面在 win 与 glass/98 下的差异（1.1.3）。"""
import os, sys
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
cfg = Path(os.environ["APPDATA"]) / "BackupStudio"

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow, NAV_ITEMS
from backupstudio.gui import stylesheet

st = AppState(TaskStore(cfg / "tasks.json"))
shots = {}
for style in ("win", "98"):
    st.set_style(style)
    app.setStyleSheet(stylesheet(st.palette))
    w = MainWindow(st); w.resize(1100, 700); w.show()
    for _ in range(10):
        app.processEvents()
    for key, label in NAV_ITEMS:
        w._select(key)
        for _ in range(6):
            app.processEvents()
        shots[(style, key)] = w.grab().toImage()
    w.close()

print("  各页面在 win vs 98 下的差异：")
for key, label in NAV_ITEMS:
    a = shots[("win", key)]
    b = shots[("98", key)]
    total = a.width() * a.height()
    d = sum(1 for y in range(0, a.height(), 4) for x in range(0, a.width(), 4)
            if a.pixel(x, y) != b.pixel(x, y))
    pct = d * 16 / total * 100
    flag = "OK" if pct > 20 else "!! 风格没生效"
    print(f"    {label:<16} {pct:5.1f}%  {flag}")
