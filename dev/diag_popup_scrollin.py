"""验证：选中靠后的项时，弹层内部是否"往下滑"以显示当前项。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="sl-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui import stylesheet
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
st = AppState(ts)
app.setStyleSheet(stylesheet(st.palette))
w = MainWindow(st)
w.resize(1000, 680)
w.move(120, 110)
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(16):
    app.processEvents()

sp = w._pages["settings"]
cb = sp.tonal_combo
print(f"  配色风格下拉 {cb.count()} 项: "
      f"{[cb.itemText(i) for i in range(cb.count())]}")

for idx in range(cb.count()):
    cb.setCurrentIndex(idx)
    for _ in range(6):
        app.processEvents()
    g = cb.mapToGlobal(QPoint(0, 0))
    cb.showPopup()
    for _ in range(10):
        app.processEvents()
    popup = cb.view().window()
    pg = popup.geometry()
    # 弹层内部滚动位置
    sb = cb.view().verticalScrollBar()
    print(f"  选中第 {idx} 项后开弹层: 弹层 y={pg.y()} "
          f"内部滚动={sb.value()}/{sb.maximum()}  "
          f"弹层相对 combo 偏移={pg.y() - (g.y() + cb.height()):+d}")
    cb.hidePopup()
    for _ in range(6):
        app.processEvents()

# 关键：弹层是否有独立的滚动条（如果列表比弹层高，就会有滚动）
print(f"\n  弹层 view 高度 = {cb.view().height()}  内容高 = {cb.view().sizeHintForRow(0)}x{cb.count()}")
print(f"  弹层高度是否受限 = {cb.view().height() < cb.count() * cb.view().sizeHintForRow(0)}")
w.close()
