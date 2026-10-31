"""分别截 设置页 / 运行记录页 在 win 与 98 下的图，供人眼对比。"""
import os, sys
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
cfg = Path(os.environ["APPDATA"]) / "BackupStudio"
OUT = R / "docs" / "screenshots" / "styles"
OUT.mkdir(parents=True, exist_ok=True)

from PySide6.QtWidgets import QApplication
app = QApplication([])
from backupstudio.store import TaskStore
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet

st = AppState(TaskStore(cfg / "tasks.json"))
for style in ("win", "98"):
    st.set_style(style)
    app.setStyleSheet(stylesheet(st.palette))
    w = MainWindow(st); w.resize(1100, 700); w.show()
    for _ in range(10):
        app.processEvents()
    for key, tag in (("settings", "settings"), ("history", "history")):
        w._select(key)
        for _ in range(8):
            app.processEvents()
        p = OUT / f"page-{tag}-{style}.png"
        w.grab().save(str(p))
        print(f"  {p.name}  {p.stat().st_size} B")
    w.close()
