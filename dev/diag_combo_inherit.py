"""确认 Card 的内联透明样式是否让子控件（下拉框）也变透明。"""
import os, sys, tempfile
from pathlib import Path

os.environ.pop("QT_QPA_PLATFORM", None)
R = Path(r"C:\APP\deepseek\work\定时备份及其压缩项目"); sys.path.insert(0, str(R))
CFG = Path(tempfile.mkdtemp(prefix="in-", dir=r"C:\APP\deepseek\work\build"))
os.environ["BACKUPSTUDIO_CONFIG_DIR"] = str(CFG)

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

app = QApplication([])
from backupstudio.gui.appstate import AppState
from backupstudio.gui.main_window import MainWindow
from backupstudio.gui import stylesheet
from backupstudio.store import TaskStore

ts = TaskStore(CFG / "tasks.json")
ts.settings.ui_style = "glass"
ts.save()
st = AppState(ts)
w = MainWindow(st)
w.resize(1100, 700)
w.show()
for _ in range(12):
    app.processEvents()
w._select("settings")
for _ in range(14):
    app.processEvents()

sp = w._pages["settings"]
cb = sp.theme_combo

# 找它所属的 Card
from backupstudio.gui.widgets import Card

p = cb.parent()
while p is not None and not isinstance(p, Card):
    p = p.parent()
print(f"  所属 Card: {p.objectName() if p else '无'}")
if p is not None:
    print(f"  Card 内联样式: {p.styleSheet()!r}")
    print(f"  Card._glass_on: {p._glass_on}")

# 从整窗截图取下拉框中心色
img = w.grab().toImage()
tl = cb.mapTo(w, QPoint(0, 0))
cx, cy = tl.x() + cb.width() // 2, tl.y() + cb.height() // 2
c = img.pixelColor(cx, cy)
edge = img.pixelColor(tl.x() + 1, cy)
print(f"\n  下拉框在窗口中 ({tl.x()},{tl.y()}) {cb.width()}x{cb.height()}")
print(f"    中心像素 = {c.name()}")
print(f"    左边缘像素 = {edge.name()}")
print(f"    期望背景 = {st.palette.surface}（glass surface）")
print(f"    期望边框 = {st.palette.border_strong}")
print(f"    尺寸 {cb.width()}x{cb.height()}  (QSS padding 6px + 边框)")

# 检查子控件是否继承了透明
print(f"\n  下拉框自身 styleSheet = {cb.styleSheet()!r}")
print(f"  下拉框 autoFillBackground = {cb.autoFillBackground()}")
from PySide6.QtCore import Qt
print(f"  WA_TranslucentBackground = "
      f"{cb.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)}")
w.close()
